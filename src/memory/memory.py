"""记忆系统：底层完全基于 memU MemoryService（memu/app/service.py）。

存储架构（memU 接管）：
  - 元数据 + 向量：SQLite（data/memu.sqlite3），memU 的 recall_file_repo /
    recall_file_segment_repo / resource_repo 三层结构（RecallFile 文档 +
    RecallFileSegment 切片 + Resource 资源），embedding 存在 segment/file 上。
  - 嵌入：memU 的 OpenAIEmbeddingSDKClient（SiliconFlow，OpenAI 兼容协议），
    经 memu_adapter.create_memu_service() 装配；可选注入 vtuber 的 LRU
    embedding 缓存（inject_embedding_cache）复用查询嵌入。
  - 检索：memU progressive_retrieve 三层渐进（segments/files/resources），
    单次 embedding + numpy cosine_topk，无 BM25 / Rerank（memU 原生不带）。

对外兼容层（MemoryManager + 模块级函数）签名与行为保持不变：main /
llm_brain / butler_agent / proactive / control_center 无需改动。映射要点：
  - commit_recall_files → memU commit_results（按 user 分组）
  - list_files / count  → recall_file_repo（sync，直接读，无需 async 桥接）
  - retrieve / get_memory_prompt → memU progressive_retrieve（segments + files）
  - delete_memories / forget_memory / clear_all → repo 直接删除（sync）
  - upsert_memory / update_archive → memU commit_results（async，sync 桥接）
  - graph_data → (files, {})：向量藏 memU repo 内部，控制中心降级无向量模式
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from src.utils import config, console

# ---------------------------------------------------------------------------
# memU 适配层（memu_adapter.create_memu_service 装配好 MemoryService 实例）
# ---------------------------------------------------------------------------
from src.memory.memu_adapter import (
    create_memu_service,
    inject_embedding_cache,
    inject_repo_cache,
)

# ---------------------------------------------------------------------------
# 枚举（兼容旧 API：层/分类语义保留，映射进 memU track/description 便于展示）
# ---------------------------------------------------------------------------


class MemoryLayer(str, Enum):
    """记忆层：越靠上越稳定、越常注入。"""

    CORE = "core"
    STATE = "state"
    PREFERENCE = "preference"
    ARCHIVE = "archive"


class MemoryCategory(str, Enum):
    """记忆分类。"""

    USER = "user"
    SELF = "self"
    WORLD = "world"
    RELATION = "relation"


# ---------------------------------------------------------------------------
# 兼容数据模型（dict-based，供 get_preference_records / fetch_relevant 使用）
# ---------------------------------------------------------------------------


class MemoryRecord:
    """单条记忆记录（兼容视图：由记忆条目投影而来）。"""

    def __init__(self, layer, category, key, value, created_at=None,
                 updated_at=None, expires_at=None) -> None:
        self.layer = layer
        self.category = category
        self.key = key
        self.value = value
        self.created_at = created_at or datetime.now().isoformat()
        self.updated_at = updated_at or datetime.now().isoformat()
        self.expires_at = expires_at

    def to_dict(self) -> dict:
        d = {
            "layer": self.layer.value, "category": self.category.value,
            "key": self.key, "value": self.value,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
        if self.expires_at:
            d["expires_at"] = self.expires_at
        return d


class ArchiveEntry:
    """历史 Session 摘要（ARCHIVE 层兼容视图）。"""

    def __init__(self, session_id, summary, period_start, period_end,
                 created_at=None) -> None:
        self.session_id = session_id
        self.summary = summary
        self.period_start = period_start
        self.period_end = period_end
        self.created_at = created_at or datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id, "summary": self.summary,
            "period_start": self.period_start, "period_end": self.period_end,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

_MEMORY_TAG_RE = re.compile(r"<memory>(.*?)</memory>", re.DOTALL)

# 记忆名规范化：小写、空白/标点 → 连字符（保留中文）
_KEBAB_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


# 记忆归属者（memU ADR 0003 user scope）：AI 自己的记忆固定为 _USER_SELF，
# 用户输入（语音识别 / 控制台）提取的记忆归当前用户名（本项目为 _USER_DEFAULT）；
# 直播弹幕等多用户场景由调用方显式传观众名（coke / 陈泽 / 超人不会飞 …）。
_USER_SELF = "self"
# 可用环境变量 MEMORY_USER_DEFAULT 覆盖（换本机用户时无需改代码）
_USER_DEFAULT = os.getenv("MEMORY_USER_DEFAULT", "chao")

# memU 默认 agent_id（与 memu_adapter VtuberUserModel 一致）
_AGENT_ID = "vtuber"


def _to_kebab(key: str) -> str:
    return _KEBAB_RE.sub("-", (key or "").strip().lower()).strip("-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _run_sync(coro):
    """在同步上下文运行 async 协程。

    - 无事件循环时（脚本 / 控制台单独进程）：asyncio.run。
    - 已在事件循环中（主程序 async 运行）：若调用方在 worker 线程，
      投递到主循环并阻塞等待（embedding client 仍在原绑定循环，安全）；
      若调用方在主线程，会阻塞——sync 方法调用 async embedding API 的固有
      限制，正常写路径应走 async 入口（commit_recall_files / update_archive_async）。
    """
    try:
        loop = asyncio.get_event_loop()
        in_loop = loop.is_running()
    except RuntimeError:
        in_loop = False
        loop = None
    if not in_loop:
        return asyncio.run(coro)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


# ---------------------------------------------------------------------------
# MemoryManager（兼容层：保持旧 API 不动，底层全部走 memU MemoryService）
# ---------------------------------------------------------------------------


class MemoryManager:
    """对外兼容层：main / llm_brain / butler_agent / proactive / control_center
    直接使用的对象。方法签名与旧 memU 文件模型一致，底层映射到 memU
    MemoryService（commit_results / progressive_retrieve / 三层 repo）。"""

    def __init__(self) -> None:
        self._service = None  # memU MemoryService（懒加载）
        self._failed = False
        self._embedding_enabled: Optional[bool] = None
        # 会话轮次（仅内存；记忆持久化由 ButlerAgent 负责）
        self.recent_turns: List[dict] = []
        self.started_at: Optional[str] = None

    # ------------------------------------------------------------------
    # service 生命周期
    # ------------------------------------------------------------------

    def _ensure_service(self):
        if self._service is None:
            if self._failed:
                raise RuntimeError("记忆系统已禁用（初始化失败）")
            try:
                self._service = _create_service()
            except Exception as e:
                self._failed = True
                console.dim(
                    f"[Memory] 记忆系统初始化失败，本次运行已禁用：{type(e).__name__}: {e}")
                raise
        return self._service

    @property
    def embedding_enabled(self) -> bool:
        if self._embedding_enabled is None:
            cfg = config.cfg
            _url = (cfg.EMBEDDING_BASE_URL or "").lower()
            # 启用真实嵌入：有 API Key（云端），或 EMBEDDING_BASE_URL 指向本地
            # llama.cpp（无 key 也可用，configured 判定与 SiliconFlowEmbeddingProvider 一致）
            self._embedding_enabled = bool(cfg.EMBEDDING_API_KEY) or (
                "127.0.0.1" in _url or "localhost" in _url)
        return self._embedding_enabled

    # ------------------------------------------------------------------
    # 会话管理（内存态）
    # ------------------------------------------------------------------

    def load(self) -> None:
        """兼容旧 API：初始化（启动时建立存储连接，失败不阻断启动）。"""
        if not self._failed:
            try:
                self._ensure_service()
            except Exception:
                pass

    def new_session(self) -> None:
        self.recent_turns = []
        self.started_at = datetime.now().isoformat()

    def add_turn(self, role: str, content: str, user: Optional[str] = None) -> None:
        """记录对话轮次（memU 蒸馏的转录源）。

        user = 消息归属者 id（memU ADR 0003 user scope）：AI 自己的消息
        （assistant/muika）缺省为 "self"；用户消息缺省为本机用户
        （_USER_DEFAULT）。直播弹幕等场景由调用方显式传观众名。
        """
        self.recent_turns.append({
            "role": role,
            "content": content,
            "user": user or (
                _USER_SELF if str(role).strip().lower() in ("assistant", "muika")
                else _USER_DEFAULT),
            "timestamp": datetime.now().isoformat(),
        })

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def count(self) -> int:
        """记忆条数（直接读 recall_file_repo，sync 高效，无需遍历分页）。"""
        try:
            svc = self._ensure_service()
            store = svc._get_database()
            files = store.recall_file_repo.list_recall_files(None)
            return len(files)
        except Exception:
            return 0

    def list_files(self, limit: int = 200, max_total: int = 1000) -> List[dict]:
        """列出记忆文件（旧格式投影：id/name/content/description/track/user/updated_at）。

        memU sqlite backend 的 _row_to_recall_file 返回基类 RecallFile（无 scope 字段），
        所以 user_id 需要从 SQLAlchemy row 直接读取。这里走 raw session 查询，
        一次拿到所有字段（含 scope 列），避免二次查询。
        """
        try:
            svc = self._ensure_service()
            store = svc._get_database()
            # 优先走 SQLAlchemy 直接查询（sqlite backend 有 _sessions）
            sessions = getattr(store, "_sessions", None)
            sqla_models = getattr(store, "_sqla_models", None)
            if sessions is not None and sqla_models is not None:
                model = sqla_models.RecallFile
                with sessions.session() as session:
                    from sqlmodel import select as _select
                    rows = session.exec(_select(model)).all()
                files: List[dict] = []
                for row in rows:
                    files.append({
                        "id": row.id,
                        "name": row.name,
                        "content": row.content or "",
                        "description": row.description or "",
                        "track": row.track or "memory",
                        "user": getattr(row, "user_id", None) or _USER_DEFAULT,
                        "updated_at": str(row.updated_at) if row.updated_at else None,
                    })
            else:
                # inmemory backend：scope 字段在 model 上
                files_dict = store.recall_file_repo.list_recall_files(None)
                files = []
                for rf in files_dict.values():
                    files.append({
                        "id": rf.id,
                        "name": rf.name,
                        "content": rf.content or "",
                        "description": rf.description or "",
                        "track": rf.track or "memory",
                        "user": getattr(rf, "user_id", None) or _USER_DEFAULT,
                        "updated_at": str(rf.updated_at) if rf.updated_at else None,
                    })
            files.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
            return files[:max(limit, 0)]
        except Exception as e:
            console.dim(f"[Memory] 列出记忆文件失败：{e}")
            return []

    def graph_data(self, limit: int = 200) -> tuple:
        """图谱数据：(files, vectors)。

        files 来自 list_files；memU 把 embedding 藏在 segment/file repo 内部，
        不便直接导出全量向量，返回空 vectors dict——控制中心会降级到无向量
        模式（只展示文件列表，不连边）。
        """
        try:
            files = self.list_files(limit=limit, max_total=max(limit * 4, 60))
            return files, {}
        except Exception as e:
            console.dim(f"[Memory] 图谱数据读取失败：{e}")
            return [], {}

    async def has_files_async(self) -> bool:
        return await asyncio.to_thread(self.count) > 0

    # ------------------------------------------------------------------
    # 写入（ButlerAgent / 会话归档）
    # ------------------------------------------------------------------

    async def commit_recall_files(self, recall_files: List[dict]) -> List[dict]:
        """批量提交记忆文件（按 user 分组，每组调一次 memU commit_results）。

        memU commit_results 按 (name, track, user scope) 做 create-or-update：
        同用户同 name 覆盖更新，否则新建；segment 自动按 content 重切。
        """
        svc = self._ensure_service()
        # 按 user 分组（同用户的文件一次提交，memU 内部按 name 去重更新）
        groups: Dict[str, List[dict]] = {}
        for f in recall_files or []:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "").strip()
            content = str(f.get("content") or "").strip()
            if not name or not content:
                continue
            user = str(f.get("user") or _USER_DEFAULT).strip()[:32] or _USER_DEFAULT
            groups.setdefault(user, []).append({
                "name": name,
                "track": f.get("track") or "memory",
                "description": f.get("description") or "",
                "content": content,
            })
        results: List[dict] = []
        for user, files in groups.items():
            try:
                r = await svc.commit_results(
                    recall_files=files,
                    user={
                        "user_id": user,
                        "agent_id": _AGENT_ID,
                        "user": user,
                    },
                )
                for rf in r.get("recall_files") or []:
                    results.append({
                        "id": rf.get("id"),
                        "memory": rf.get("content") or "",
                        "event": "COMMIT",
                    })
            except Exception as e:
                console.dim(f"[Memory] 提交记忆文件失败（user={user}）：{e}")
        # 记忆已变更：导出图谱快照（供控制中心跨进程读取）
        export_graph_data()
        return results

    async def update_archive_async(self, summary: str, period_start: str = "",
                                   period_end: str = "") -> None:
        """会话摘要 → ARCHIVE 记忆（track=archive，归属 AI 自己 self）。"""
        if not summary:
            return
        await asyncio.to_thread(self._add_archive_sync, summary, period_start, period_end)

    def _add_archive_sync(self, summary: str, period_start: str, period_end: str) -> None:
        try:
            svc = self._ensure_service()
            name = f"archive-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            _run_sync(svc.commit_results(
                recall_files=[{
                    "name": name,
                    "track": "archive",
                    "description": f"archive / {period_start[:10]} → {period_end[:10]}",
                    "content": summary,
                }],
                user={
                    "user_id": _USER_SELF,
                    "agent_id": _AGENT_ID,
                    "user": _USER_SELF,
                },
            ))
        except Exception as e:
            console.dim(f"[Memory] 写入会话摘要失败：{e}")

    # ------------------------------------------------------------------
    # 兼容存根（旧 API 保留，避免调用方崩溃）
    # ------------------------------------------------------------------

    def clear_all(self) -> None:
        """清空全部记忆（直接清三层 repo：recall_file / segment / resource）。"""
        try:
            svc = self._ensure_service()
            store = svc._get_database()
            store.recall_file_repo.clear_recall_files(None)
            store.recall_file_segment_repo.clear_segments(None)
            store.resource_repo.clear_resources(None)
        except Exception as e:
            console.dim(f"[Memory] 清空记忆失败：{e}")

    def upsert_memory(self, layer: str, category: str, key: str, value: str,
                      expires_at: Optional[str] = None) -> str:
        """写入一条兼容记忆（layer/category 进 description，value 为内容）。

        映射成单个 recall_file 调 memU commit_results（create-or-update by name）。
        """
        try:
            svc = self._ensure_service()
            description = f"{layer} / {category}"
            if expires_at:
                description += f" / expires {expires_at}"
            r = _run_sync(svc.commit_results(
                recall_files=[{
                    "name": _to_kebab(key) or f"{layer}-{_md5(key)[:8]}",
                    "track": "memory",
                    "description": description,
                    "content": value,
                }],
                user={
                    "user_id": _USER_DEFAULT,
                    "agent_id": _AGENT_ID,
                    "user": _USER_DEFAULT,
                },
            ))
            files = r.get("recall_files") or []
            return files[0].get("id") if files else ""
        except Exception as e:
            console.dim(f"[Memory] upsert_memory 失败：{e}")
            return ""

    def forget_memory(self, key: str) -> None:
        """按 key（转 kebab name）删除记忆文件 + 关联 segment。"""
        target = _to_kebab(key)
        if not target:
            return
        try:
            svc = self._ensure_service()
            store = svc._get_database()
            # 按 name 匹配（recall_file_repo 无 name 过滤便捷方法，全量扫一次）
            for rf in store.recall_file_repo.list_recall_files(None).values():
                if rf.name == target:
                    store.recall_file_segment_repo.delete_segments_for_file(rf.id)
                    store.recall_file_repo.clear_recall_files({"id": rf.id})
            export_graph_data()
        except Exception as e:
            console.dim(f"[Memory] forget_memory 失败：{e}")

    def delete_memories(self, ids: List[str]) -> int:
        """按 id 批量删除记忆条目（有序遗忘底层能力），返回成功删除数。

        直接操作 repo：recall_file_repo 按 id 删文件 + segment_repo 删关联切片。
        id 不存在/已删静默跳过（幂等）；单条失败不影响其余。
        """
        ids = [str(i) for i in ids if i]
        if not ids:
            return 0
        deleted = 0
        try:
            svc = self._ensure_service()
            store = svc._get_database()
            for mid in ids:
                try:
                    store.recall_file_segment_repo.delete_segments_for_file(mid)
                    store.recall_file_repo.clear_recall_files({"id": mid})
                    deleted += 1
                except Exception:
                    continue
        except Exception as e:
            console.dim(f"[Memory] 批量删除记忆失败：{e}")
        export_graph_data()
        # 库为空时 export_graph_data 跳过写盘，显式从快照中移除已删 id
        _strip_graph_export(ids)
        return deleted

    async def delete_memories_async(self, ids: List[str]) -> int:
        return await asyncio.to_thread(self.delete_memories, ids)

    def add_archive(self, session_id: str, summary: str, period_start: str,
                    period_end: str) -> None:
        """旧 API 保留：写入会话归档（name=archive-<session>）。"""
        try:
            svc = self._ensure_service()
            _run_sync(svc.commit_results(
                recall_files=[{
                    "name": f"archive-{_to_kebab(session_id)[:40] or 'session'}",
                    "track": "archive",
                    "description": f"archive / {period_start[:10]} → {period_end[:10]}",
                    "content": summary,
                }],
                user={
                    "user_id": _USER_SELF,
                    "agent_id": _AGENT_ID,
                    "user": _USER_SELF,
                },
            ))
        except Exception as e:
            console.dim(f"[Memory] add_archive 失败：{e}")

    def update_archive(self, summary: str, period_start: str = "",
                       period_end: str = "") -> None:
        self._add_archive_sync(summary, period_start, period_end)

    def get_preference_records(self) -> List[MemoryRecord]:
        """PREFERENCE 层兼容视图（description 以 preference 开头的记忆）。"""
        records: List[MemoryRecord] = []
        try:
            for f in self.list_files(limit=10000, max_total=10000):
                desc = f.get("description") or ""
                if desc.startswith("preference"):
                    records.append(MemoryRecord(
                        layer=MemoryLayer.PREFERENCE,
                        category=MemoryCategory.USER,
                        key=f.get("name") or "",
                        value=f.get("content") or "",
                        updated_at=str(f.get("updated_at")) if f.get("updated_at") else None,
                    ))
        except Exception:
            pass
        return records

    def get_archives(self) -> List[ArchiveEntry]:
        """ARCHIVE 层兼容视图（track=archive 或 description 以 archive 开头）。"""
        entries: List[ArchiveEntry] = []
        try:
            for f in self.list_files(limit=10000, max_total=10000):
                track = f.get("track") or ""
                desc = f.get("description") or ""
                if track == "archive" or desc.startswith("archive"):
                    entries.append(ArchiveEntry(
                        session_id=f.get("name") or "",
                        summary=f.get("content") or "",
                        period_start="", period_end="",
                        created_at=str(f.get("updated_at")) if f.get("updated_at") else None,
                    ))
        except Exception:
            pass
        return entries

    async def get_memory_prompt(self, query: str = "", top_k: int = 8) -> str:
        """把相关记忆格式化成 system prompt 注入段（segments 层，async-first）。

        走 memU progressive_retrieve，取 segments 切片格式化成 `- text` 列表。
        """
        try:
            if not query or not query.strip():
                return ""
            svc = self._ensure_service()
            data = await svc.progressive_retrieve(
                query,
                where={"user_id__in": [_USER_DEFAULT, _USER_SELF]},
            )
            segments = data.get("segments") or []
            lines = []
            for seg in segments[:top_k]:
                text = (seg.get("text") or "").strip()
                if text:
                    lines.append(f"- {text}")
            return "\n".join(lines)
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# 模块级函数（对外使用面不变）
# ---------------------------------------------------------------------------

_manager: Optional[MemoryManager] = None


def _create_service():
    """创建 memU MemoryService（经 memu_adapter 装配 + 注入两层缓存）。

    memu_adapter.create_memu_service() 负责把 vtuber 的 EMBEDDING_* 配置转成
    memU 的 EmbeddingProfilesConfig（SiliconFlow sdk backend）+ sqlite
    DatabaseConfig + VtuberUserModel（chao/self 归属）。两层缓存注入：
      - inject_repo_cache：segment / file 全量加载结果 TTL 缓存（30s），
        避免每次检索都反序列化 ~45MB JSON，检索延迟从 ~1s 降到 ~100ms；
      - inject_embedding_cache：vtuber LRU embedding 缓存，查询嵌入命中
        缓存 0ms，未命中回填。
    """
    service = create_memu_service()
    # 静默注入缓存（import 失败 / 未配置时返回原 service，纯优化不阻断）
    try:
        service = inject_repo_cache(service)
    except Exception:
        pass
    try:
        service = inject_embedding_cache(service)
    except Exception:
        pass
    return service


def get_manager() -> MemoryManager:
    global _manager
    if _manager is None:
        _manager = MemoryManager()
    return _manager


def export_graph_data() -> Optional[str]:
    """导出图谱快照（files + 向量）到 data/memory_graph.json，返回路径或 None。

    控制中心只读文件快照，避免跨进程直接访问 sqlite 的并发问题。写入用
    「临时文件 + 原子替换」，控制中心不会读到半截文件。库为空时跳过写盘
    （避免误清空已有快照）。
    """
    try:
        mm = get_manager()
        files, vectors = mm.graph_data(limit=200)
        if not files:
            return None
        path = os.path.join(config.cfg.PROJECT_ROOT, "data", "memory_graph.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"files": files, "vectors": vectors, "exported_at": _now_iso()},
                f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
        return path
    except Exception as e:
        console.dim(f"[Memory] 图谱数据导出失败：{e}")
        return None


def _strip_graph_export(ids: List[str]) -> None:
    """从图谱快照 data/memory_graph.json 中剔除已删除的 id。

    库为空时 export_graph_data 会跳过写盘（`if not files: return None`），
    若快照里的条目在库中已不存在（快照过期/库被清空），将永远删不掉——
    这里显式把已删 id 从 files 中移除并原子写回。
    """
    try:
        path = os.path.join(config.cfg.PROJECT_ROOT, "data", "memory_graph.json")
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("files") or []
        if not any(f.get("id") in ids for f in files):
            return
        data["files"] = [f for f in files if f.get("id") not in ids]
        vectors = data.get("vectors")
        if isinstance(vectors, dict):
            data["vectors"] = {k: v for k, v in vectors.items() if k not in ids}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        console.dim(f"[Memory] 快照剔除失败：{e}")


def count() -> int:
    return get_manager().count()


def extract_and_strip(text: str) -> str:
    """剥离 <memory>...</memory> 标签块（供句子清洗，标签内容不参与朗读）。"""
    if not text:
        return text
    return _MEMORY_TAG_RE.sub(" ", text)


# ===== 记忆注入 standing instruction（严格参照 memU hosts/instruction.py） =====
# 对标 memU RETRIEVAL_BODY：回答前检索、查询可改写、三层渐进（segments/files）、
# fail-open（返回空就正常回答，绝不编造记忆）。该段常驻系统提示（与 memU 把指令
# 打进宿主全局指令文件同语义），检索结果按需注入其下。
STANDING_INSTRUCTION = """\
### 记忆使用说明

回答前已为你检索与当前话题相关的记忆，注入段在下方。若注入段为空 = 没有相关记忆，
正常回答即可，不要编造或臆测记忆内容。

注入的记忆按两层渐进展开：
- `segments`：最窄、最贴题的记忆切片，每条标注相关度（score，越接近 1 越可信）与来源。
- `files`：来源文档的汇总（summary）；某条 segment 太单薄、需要更多背景时，再查阅对应
  来源的 `content`。

先把 segments 作为主要依据；记忆可能过时，与用户当前说法冲突时以当前说法为准。"""


async def _retrieve(query: str, top_k: int = 8, user: str = _USER_DEFAULT) -> str:
    """检索相关记忆并按 memU 三层形状（segments/files）格式化注入段。

    对标 memU hosts/retrieval.py _shape_for_agent：
    - segments：命中切片，带 score 与来源标识（来源文件的 name）
    - files：来源文档 roll-up（同源合并，score 取段最大），带 summary（description）
      与 content

    user = 当前交互者 id（memU ADR 0003 user scope）：只检索该用户的记忆
    加 AI 自己的记忆（self）——coke 提问时不会带出陈泽的记忆。
    """
    svc = get_manager()._ensure_service()
    data = await svc.progressive_retrieve(
        query,
        where={"user_id__in": [user, _USER_SELF]},
    )
    segments = data.get("segments") or []
    files = data.get("files") or []
    file_by_id: Dict[str, dict] = {f.get("id"): f for f in files}

    seg_lines: List[str] = []
    file_rollup: Dict[str, dict] = {}
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        fid = seg.get("recall_file_id")
        f = file_by_id.get(fid) or {}
        name = f.get("name") or _to_kebab(text)[:24] or "未命名"
        u = str(seg.get("user_id") or f.get("user_id") or _USER_DEFAULT)[:32]
        score = seg.get("score")
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "-"
        seg_lines.append(f"- [score {score_s}]（来源：{name}，user：{u}）{text}")
        # files 层：同源（同 file）合并，score 取 max，summary 优先 description
        if fid not in file_rollup:
            desc = (f.get("description") or "").strip()
            # summary 优先语义化 description；ButlerAgent 常存 "extracted / assistant"
            # 这类 layer/category 标签（无语义），此时退回记忆文本前 60 字
            if (not desc or len(desc) < 12
                    or ("extracted" in desc.lower() and "/" in desc)):
                desc = text[:60] + ("…" if len(text) > 60 else "")
            file_rollup[fid] = {
                "name": name,
                "summary": desc,
                "content": (f.get("content") or text).strip(),
                "score": float(score or 0.0),
                "user": u,
            }
        else:
            file_rollup[fid]["score"] = max(
                file_rollup[fid]["score"], float(score or 0.0))
    if not seg_lines:
        return ""
    parts = ["segments:"]
    parts.extend(seg_lines)
    parts.append("")
    parts.append("files:")
    for fv in sorted(file_rollup.values(), key=lambda x: x["score"], reverse=True):
        parts.append(
            f"- 来源：{fv['name']}（score {fv['score']:.2f}，user：{fv['user']}）\n"
            f"  summary：{fv['summary']}\n"
            f"  content：{fv['content']}")
    return "\n".join(parts)


# 检索超时（秒）：超时跳过记忆注入，避免 embedding 网络调用阻塞 LLM 首字延迟。
# 启动期 warmup 还没 embedding 首包回来时，真实检索会独占一次网络往返（~2s），
# 这直接变成了用户感知的"首字延迟"。所以分两档：
#   - 正常（warmup 已完成/命中缓存）：检索 <100ms，超时 3s 给网络抖动留余量
#   - 启动头 30 秒（warmup 首包可能还在路上）：超时 0.5s 快速跳过，不拖首字
_MEM_RETRIEVE_TIMEOUT = float(os.getenv("MEMORY_RETRIEVE_TIMEOUT", "3.0"))
_MEM_RETRIEVE_BOOT_WINDOW_S = 30.0
_MEM_RETRIEVE_BOOT_TIMEOUT_S = 0.5

# warmup 完成标志：warmup() 成功跑过一次 progressive_retrieve 后置 True，
# retrieve() 据此决定使用哪一档超时。
_WARMUP_DONE: bool = False
_WARMUP_STARTED_AT: Optional[float] = None


async def warmup() -> None:
    """预热记忆检索管线（embedding 连接 + 存储建连），后台启动一次。

    成功完成一次 progressive_retrieve 后置 _WARMUP_DONE=True；此后真实检索
    的 query embedding 会命中 SiliconFlow HTTP 连接池 + LRU 缓存，
    延迟从 ~2s 降到 <100ms（命中 LRU 时 ~50ms）。
    静默失败，不影响启动。
    """
    global _WARMUP_DONE, _WARMUP_STARTED_AT
    if _WARMUP_STARTED_AT is not None:   # 幂等
        return
    import time as _t
    _WARMUP_STARTED_AT = _t.perf_counter()
    try:
        svc = get_manager()._ensure_service()
        # 记忆库为空：没有可预热的检索，直接标记完成（省一次白嵌入）
        if count() == 0:
            _WARMUP_DONE = True
            return
        await asyncio.wait_for(
            svc.progressive_retrieve("预热", where={}),
            timeout=60,
        )
        _WARMUP_DONE = True
    except Exception:
        # 预热失败也不打断启动；让真实检索走"慢路径 + 超时跳过"兜底
        pass


async def retrieve(user_text: str, top_k: int = 8,
                   user: str = _USER_DEFAULT) -> str:
    """检索相关记忆并格式化成 prompt 注入段（llm_brain 调用）。

    user = 当前交互者 id（缺省本机用户）；只召回该用户 + AI 自己（self）的
    记忆。嵌入不可用/检索失败时回退 LLM 语义筛选（ButlerAgent.fetch_relevant）。
    检索整体限时 _MEM_RETRIEVE_TIMEOUT 秒：超时直接跳过记忆注入，保证
    记忆检索不拖慢「用户提问 → LLM 首字」的关键路径（首帧延迟优化）。
    """
    if not user_text or not user_text.strip():
        return ""
    import time as _time
    t0 = _time.perf_counter()
    # 选择超时档位：启动头 30s 且 warmup 没完成 → 0.5s 快速跳过；否则正常 3s
    if _WARMUP_STARTED_AT is None:
        # warmup 根本没启动过（memory_warmup 未开）→ 永远用 0.5s 激进档，
        # 不让 embedding 首包拖首字延迟。
        effective_timeout = _MEM_RETRIEVE_BOOT_TIMEOUT_S
    elif not _WARMUP_DONE:
        elapsed_s = _time.perf_counter() - _WARMUP_STARTED_AT
        if elapsed_s < _MEM_RETRIEVE_BOOT_WINDOW_S:
            effective_timeout = _MEM_RETRIEVE_BOOT_TIMEOUT_S
        else:
            # 超过窗口但 warmup 仍没完成 → embedding 可能不可用，激进跳过
            effective_timeout = _MEM_RETRIEVE_BOOT_TIMEOUT_S
    else:
        effective_timeout = _MEM_RETRIEVE_TIMEOUT
    try:
        # 记忆库为空：直接跳过嵌入与检索（否则每轮对话都白调一次
        # embedding，空库命中不了任何东西，纯浪费 ~300ms 网络往返）
        if count() == 0:
            console.dim(
                "  [Memory] 未命中（记忆库为空：先和 AI 聊几句，"
                "会话结束后 ButlerAgent 会自动蒸馏记忆）")
            return ""
        result = await asyncio.wait_for(
            _retrieve(user_text, top_k, user),
            timeout=effective_timeout)
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        if result:
            seg_count = result.count("\n- [score")
            # 命中有结果：console 可见日志，用户能看到记忆系统在工作
            console.dim(f"  [Memory] 检索命中 {seg_count} 条（{elapsed_ms:.0f} ms）")
            return result
        # 0 条命中：同样打可见日志，说明"记忆系统是活的，只是没匹配到"
        total_files = count()
        if total_files == 0:
            console.dim(
                "  [Memory] 未命中（记忆库为空：先和 AI 聊几句，"
                "会话结束后 ButlerAgent 会自动蒸馏记忆）")
        else:
            console.dim(
                f"  [Memory] 未命中（{elapsed_ms:.0f} ms，库内共 {total_files} 个文件）")
    except asyncio.TimeoutError:
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        boot_msg = "" if _WARMUP_DONE else f"（启动保护期，超时阈值 {effective_timeout:.1f}s）"
        console.dim(
            f"  [Memory] 检索超时 {boot_msg}，跳过记忆注入"
            f"（{elapsed_ms:.0f} ms，阈值 {effective_timeout:.1f}s）")
        return ""
    except Exception as e:
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        console.dim(
            f"  [Memory] 检索失败（{elapsed_ms:.0f} ms），回退 LLM 筛选：{e}")
    return await _llm_fallback(user_text, user)


async def aclose() -> None:
    """关闭 embedding async 连接（进程退出时调用，避免 Event loop is closed 警告）。

    memU MemoryService 没有 aclose 方法，但底层 OpenAIEmbeddingSDKClient 持有
    AsyncOpenAI 实例（.client 属性），关闭它即可释放 httpx 连接。失败静默。
    """
    try:
        svc = get_manager()._ensure_service()
        client = svc._get_embedding_client("embedding")
        inner = getattr(client, "client", None)  # AsyncOpenAI
        if inner is not None and hasattr(inner, "close"):
            await inner.close()
    except Exception:
        pass


async def _llm_fallback(query: str, user: str = _USER_DEFAULT) -> str:
    """LLM 回退检索：从该用户（+self）的记忆中筛出与查询相关的条目。"""
    try:
        from src.llm.butler_agent import ButlerAgent
        # max_total 与 limit 同量级：回退路径只需筛选 top 候选（再交给 LLM
        # 语义过滤），全量拉取只会撑爆 LLM 上下文
        files = get_manager().list_files(limit=200, max_total=200)
        files = [f for f in files
                 if (f.get("user") or _USER_DEFAULT) in (user, _USER_SELF)]
        if not files:
            return ""
        prefs = [
            MemoryRecord(layer=MemoryLayer.CORE, category=MemoryCategory.USER,
                         key=f.get("name") or "", value=f.get("content") or "")
            for f in files
        ]
        butler = ButlerAgent()
        matched = await butler.fetch_relevant(query, prefs)
        if not matched:
            return ""
        segs = [f"- [score -]（来源：{r.key or '未命名'}）{r.value}"
                for r in matched[:8]]
        return "segments:\n" + "\n".join(segs)
    except Exception as e:
        console.dim(f"[Memory] LLM 回退检索失败：{e}")
        return ""


def remember(layer: str, category: str, key: str, value: str,
             expires_at: Optional[str] = None) -> str:
    return get_manager().upsert_memory(layer, category, key, value, expires_at)


def forget(key: str) -> None:
    get_manager().forget_memory(key)


def clear_all() -> None:
    get_manager().clear_all()


def save_session_summary(summary: str, period_start: str = "",
                        period_end: str = "") -> None:
    get_manager().update_archive(summary, period_start, period_end)


def extract_tags(text: str) -> List[str]:
    """提取 <memory> 标签内文本（旧 API 保留）。"""
    return [m.strip() for m in _MEMORY_TAG_RE.findall(text or "") if m.strip()]


def get_context(query: str = "", top_k: int = 8) -> str:
    """同步版记忆检索（旧 API 保留，仅限无事件循环的线程/脚本调用）。

    内部用 asyncio.run 驱动 async 检索链；主程序（已有事件循环）请直接用
    异步 retrieve()。
    """
    if not query or not query.strip():
        return ""
    try:
        return asyncio.run(_retrieve(query, top_k))
    except Exception:
        return ""


__all__ = [
    "MemoryLayer", "MemoryCategory", "MemoryRecord", "ArchiveEntry",
    "MemoryManager",
    "get_manager", "count", "retrieve", "warmup", "aclose",
    "remember", "forget", "clear_all",
    "extract_tags", "extract_and_strip", "get_context", "save_session_summary",
    "export_graph_data", "STANDING_INSTRUCTION",
]
