"""vtuber 记忆层：基于 memU 引擎（src/memory/memU-main）的全新封装。

memU 只负责存储与向量检索（三入口：list_all_recall_files /
progressive_retrieve / commit_results，见 memU-main/AGENTS.md），
本模块在其上补充 vtuber 特有的会话态与记忆维护：

- 会话轮次：add_turn / recent_turns / started_at / new_session
- 记忆文件：commit_recall_files / list_files / graph_data / delete / clear
- 显式记忆与遗忘：remember_explicit / forget_phrase
- 时间衰减：decay_stale_memories / decay_loop
- 检索注入：retrieve / get_memory_prompt（向量优先，关键词回退）

设计要点：
- memU 的 sqlite / embedding 后端均为惰性加载（factory 只在用到时
  import sqlmodel / openai 等），因此缺依赖的 UI 环境（runtime）也能
  导入本模块；对应功能不可用时走降级路径而不是启动崩溃。
- 记忆归属采用 memU ADR 0003 user scope：在默认 user_id/agent_id 之上
  扩展 vtuber 的归属者字段 user（self / chao / 观众名），检索与写入
  均按 user 过滤，互不串扰。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# ---------- 常量（vtuber 语义，供调用方 / 图谱 / 测试引用） ----------

# 记忆归属者：AI 自己的记忆固定为 self；用户输入提取的记忆归默认用户
_USER_SELF = "self"
_USER_DEFAULT = os.getenv("MEMORY_USER_DEFAULT", "chao")
_AGENT_ID = "vtuber"

# 会话保留的最大轮数（超出丢弃最旧）
_MAX_TURNS = 60
# 记忆文件过期天数（超期未更新则被衰减清理）
_MEMORY_TTL_DAYS = int(os.getenv("MEMORY_TTL_DAYS", "60"))
# 语义去重阈值（向量余弦相似度）：新记忆与同归属者已有记忆的相似度达到
# 该值视为近似重复跳过——防止同一事实的不同说法反复入库污染检索
_SEMANTIC_DUP_THRESHOLD = 0.9
# 图谱快照路径（控制中心跨进程只读）
_GRAPH_EXPORT_REL = os.path.join("data", "memory_graph.json")

# 检索结果注入 prompt 的固定说明（llm_brain 组装 system prompt 用）
STANDING_INSTRUCTION = """\
### 记忆使用说明
- 以下是从我的长期记忆中检索到的相关内容，可能与当前话题相关。
- 用它们作为背景知识自然地融入回答，不要机械复述、不要逐条罗列。
- 若没有相关记忆，忽略本段即可。
"""

# ---------- memU 引擎路径注入（顶层执行，惰性 import 由使用时触发） ----------

_MEMU_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memU-main", "src")


def _ensure_memu_path() -> None:
    """把 memU 引擎源码目录加入 sys.path，使顶层 import memu 可用。"""
    if _MEMU_SRC not in sys.path:
        sys.path.insert(0, _MEMU_SRC)


_ensure_memu_path()

from src.utils import config, console  # noqa: E402


class MemoryManager:
    """基于 memU MemoryService 的记忆管理器（vtuber 语义封装）。

    - 写入 / 检索走 memU 三入口（commit_results / progressive_retrieve）
    - 列出 / 删除 / 统计直连 repo（同步，见 memU database 层）
    - 会话态（recent_turns / started_at）为进程内数据，不落库
    """

    def __init__(self) -> None:
        self._turns: list[dict] = []
        self.started_at: str | None = None
        self._service: Any = None
        self._service_lock = threading.Lock()
        # 会话轮次锁：读写 recent_turns 都要拿锁（后台 agent 只读快照）
        self._turns_lock = threading.RLock()
        # memU 服务 IO 锁：主事件循环与后台线程（memory_tools 经
        # asyncio.to_thread 写记忆）可能并发访问 sqlite，串行化防脏数据
        self._io_lock = threading.RLock()

    # ---------- service 生命周期 ----------

    @property
    def embedding_enabled(self) -> bool:
        """是否有可用的 embedding 配置（本地端点免 Key，与 embedding.py 一致）。

        决定记忆能否写入 / 向量检索；不可用时写入降级跳过、检索走关键词回退。
        """
        base_url = (config.cfg.EMBEDDING_BASE_URL or "").strip()
        has_key = bool(config.cfg.EMBEDDING_API_KEY or config.cfg.SILICONFLOW_API_KEY)
        return has_key or _is_local_url(base_url)

    def _ensure_embedding(self) -> bool:
        """embedding 可用性提示（首次不可用时一次性 console.dim，避免每轮刷屏）。"""
        if self.embedding_enabled:
            return True
        if not getattr(self, "_embed_warned", False):
            self._embed_warned = True
            console.dim("嵌入服务未配置，记忆写入已跳过"
                        "（配置 EMBEDDING_BASE_URL / EMBEDDING_API_KEY 后自动生效）")
        return False

    def _build_service(self):
        """按项目配置构建 memU MemoryService（sqlite 持久化 + SiliconFlow 向量）。

        memU 及 sqlite/openai 依赖全部在此惰性导入：缺依赖的环境（如仅装
        UI 的 runtime）即使调用到本方法，导入失败也会以异常形式向上抛出，
        由调用方降级处理，而不是在模块导入期就崩溃。
        """
        from memu.app.settings import (
            DatabaseConfig,
            DefaultUserModel,
            EmbeddingConfig,
            EmbeddingProfilesConfig,
            MetadataStoreConfig,
            UserConfig,
        )
        from memu.app.service import MemoryService

        class _VtuberUserModel(DefaultUserModel):
            """在 memU 默认 scope（user_id / agent_id）之上增加归属者
            字段 user（self / chao / 观众名），记忆按它分组与过滤。"""

            user: str | None = None

        db_dir = os.path.join(config.cfg.PROJECT_ROOT, "data")
        os.makedirs(db_dir, exist_ok=True)
        db_dsn = f"sqlite:///{os.path.join(db_dir, 'memu.sqlite3')}"
        embedding = EmbeddingConfig(
            provider="openai",  # SDK 后端：任意 OpenAI 兼容端点
            client_backend="sdk",
            base_url=config.cfg.EMBEDDING_BASE_URL,
            api_key=config.cfg.EMBEDDING_API_KEY or "local",
            embed_model=config.cfg.EMBEDDING_MODEL,
            embed_batch_size=8,
        )
        return MemoryService(
            database_config=DatabaseConfig(
                metadata_store=MetadataStoreConfig(provider="sqlite", dsn=db_dsn)
            ),
            embedding_profiles=EmbeddingProfilesConfig(
                root={"default": embedding}
            ),
            user_config=UserConfig(model=_VtuberUserModel),
        )

    def _ensure_service(self):
        """懒构建 service（线程安全）；失败抛出以便调用方降级。"""
        if self._service is None:
            with self._service_lock:
                if self._service is None:
                    self._service = self._build_service()
        return self._service

    def load(self) -> None:
        """启动时初始化存储（失败仅告警，记忆功能降级为不可用）。"""
        try:
            self._ensure_service()
        except Exception as e:
            console.warn(f"记忆系统不可用（本次会话无长期记忆）：{e}")

    def new_session(self) -> None:
        """开启新一轮会话：清空进程内轮次并记录起始时间。"""
        with self._turns_lock:
            self._turns = []
        self.started_at = datetime.now().isoformat()

    # ---------- 会话轮次 ----------

    @property
    def recent_turns(self) -> list[dict]:
        """会话轮次快照：返回副本，防后台线程与主循环并发读写改坏列表。"""
        with self._turns_lock:
            return list(self._turns)

    def add_turn(self, role: str, content: str, user: str | None = None,
                 source: str | None = None) -> None:
        """记录一轮对话。AI 的发言归属 self，其余归传入的 user（默认用户）。

        source：可选来源标记（"proactive" 表示 agent 主动发言等），供记忆层
        后续按归属区分权重，不影响既有读取逻辑。
        """
        owner = (
            _USER_SELF
            if str(role).strip().lower() in ("assistant", "muika")
            else (user or _USER_DEFAULT)
        )
        with self._turns_lock:
            self._turns.append({
                "role": role,
                "content": str(content or "").strip(),
                "user": owner,
                "source": source,
                "timestamp": datetime.now().isoformat(),
            })
            if len(self._turns) > _MAX_TURNS:
                self._turns = self._turns[-_MAX_TURNS:]
            if self.started_at is None and self._turns:
                self.started_at = self._turns[0]["timestamp"]

    # ---------- 归属与文件工具 ----------

    @staticmethod
    def _scope(user: str | None = None) -> dict[str, str]:
        """把 vtuber 归属者映射为 memU user scope（写入 commit 用）。"""
        owner = (user or _USER_DEFAULT).strip()[:32] or _USER_DEFAULT
        return {"user": owner, "agent_id": _AGENT_ID, "user_id": owner}

    @staticmethod
    def _file_dict(record) -> dict:
        """memU RecallFile → 对外文件字典（图谱 / 列表 / 回退检索通用）。"""
        return {
            "id": record.id,
            "name": record.name or "",
            "description": record.description or "",
            "content": record.content or "",
            "user": getattr(record, "user", None) or _USER_DEFAULT,
            "track": record.track or "memory",
            "created_at": str(record.created_at) if record.created_at else None,
            "updated_at": str(record.updated_at) if record.updated_at else None,
        }

    def _walk_files(self) -> list[dict]:
        """列出全部记忆文件（所有归属者），按更新时间倒序。

        memU repo 读回的模型不含 scope 字段（基类 RecallFile 丢弃 user），
        因此直接用 SQLModel 查原始表拿到真实 user 归属，保证图谱分组准确；
        查询失败时降级为按默认用户 + AI 自己分组回填。
        """
        service = self._ensure_service()
        with self._io_lock:
            files: list[dict] = []
            try:
                # sqlmodel 懒加载：仅在有该依赖的环境（主程序）才可用
                from sqlmodel import Session, select
                model = service.database._sqla_models.RecallFile
                with Session(service.database._sessions.engine) as session:
                    rows = session.exec(select(model)).all()
                for row in rows:
                    files.append({
                        "id": row.id,
                        "name": row.name or "",
                        "description": row.description or "",
                        "content": row.content or "",
                        "user": getattr(row, "user", None) or _USER_DEFAULT,
                        "track": row.track or "memory",
                        "created_at": str(row.created_at) if row.created_at else None,
                        "updated_at": str(row.updated_at) if row.updated_at else None,
                    })
            except Exception:
                for owner in (_USER_DEFAULT, _USER_SELF):
                    rows = service.database.recall_file_repo.list_recall_files(
                        {"user": owner}
                    ).values()
                    for record in rows:
                        item = self._file_dict(record)
                        item["user"] = owner
                        files.append(item)
            files.sort(key=lambda f: f.get("updated_at") or "", reverse=True)
            return files

    # ---------- 查询 ----------

    def count(self) -> int:
        """记忆文件总数（不可用时返回 0）。"""
        try:
            return len(self._walk_files())
        except Exception:
            return 0

    def list_files(self, limit: int = 200, max_total: int = 1000) -> list[dict]:
        """列出记忆文件（最新优先），最多返回 min(limit, max_total) 条。"""
        files = self._walk_files()
        return files[: min(limit, max_total)]

    def graph_data(self, limit: int = 200) -> tuple[list[dict], None]:
        """图谱数据：同 list_files，返回 (files, next_cursor=None)。"""
        return self.list_files(limit=limit), None

    async def get_memory_prompt(self, query: str = "", top_k: int = 8) -> str:
        """检索相关记忆并格式化为 prompt 注入段（主动发言等场景）。"""
        return await retrieve(query or "", top_k=top_k)

    # ---------- 写入 ----------

    async def commit_recall_files(self, files: list[dict]) -> dict:
        """按归属分组写入记忆文件（memU commit_results，name 幂等）。

        写入前两级去重，避免重复提取污染检索：
        1. 精确去重：同用户已存在相同内容的记忆跳过；
        2. 语义去重：与同用户已有记忆向量相似度 ≥ 阈值（近似重复）跳过。
        """
        if not self._ensure_embedding():
            return {"recall_files": []}
        service = self._ensure_service()
        with self._io_lock:
            # 已有内容集合（精确去重基准）：user → {content}；查询失败则降级不去重
            existing: dict[str, set[str]] = {}
            try:
                for row in self._walk_files():
                    owner = str(row.get("user") or _USER_DEFAULT)
                    existing.setdefault(owner, set()).add((row.get("content") or "").strip())
            except Exception:
                existing = {}
            grouped: dict[str, list[dict]] = {}
            for item in files:
                owner = str(item.get("user") or _USER_DEFAULT).strip()[:32] or _USER_DEFAULT
                content = (item.get("content") or "").strip()
                if not content or content in existing.get(owner, set()):
                    continue  # 空内容或同用户已有相同内容 → 跳过
                if await self._semantic_duplicate(content, owner):
                    console.dim(f"[记忆] 与已有记忆近似重复，跳过：{content[:24]}")
                    continue
                existing.setdefault(owner, set()).add(content)  # 同批内也去重
                grouped.setdefault(owner, []).append(item)
            if not grouped:
                return {"recall_files": []}
            committed: list[dict] = []
            for owner, items in grouped.items():
                result = await service.commit_results(recall_files=items, user=self._scope(owner))
                committed.extend(result.get("recall_files") or [])
        export_graph_data()
        return {"recall_files": committed}

    async def _semantic_duplicate(self, content: str, owner: str) -> bool:
        """语义去重：与同归属者已有记忆高度相似（余弦 ≥ 阈值）视为重复。

        用 memU 向量检索对已有记忆做相似度召回，最高分达到阈值即跳过；
        embedding / 检索异常时保守返回 False（照常写入，不让去重拖垮记忆）。
        """
        with self._io_lock:
            try:
                service = self._ensure_service()
                data = await service.progressive_retrieve(
                    content, where={"user": owner})
            except Exception:
                return False
            for seg in data.get("segments") or []:
                score = seg.get("score")
                if isinstance(score, (int, float)) and float(score) >= _SEMANTIC_DUP_THRESHOLD:
                    return True
            return False

    async def delete_memories_async(self, ids: list[str]) -> int:
        """异步删除（供 async 调用方，内部走线程池）。"""
        return await asyncio.to_thread(self.delete_memories, ids)

    def delete_memories(self, ids: list[str]) -> int:
        """按 id 删除记忆文件及其切片，返回删除条数。"""
        id_list = [str(i) for i in ids if i]
        if not id_list:
            return 0
        service = self._ensure_service()
        with self._io_lock:
            deleted = service.database.recall_file_repo.clear_recall_files(
                {"id__in": id_list}
            )
            service.database.recall_file_segment_repo.clear_segments(
                {"recall_file_id__in": id_list}
            )
        export_graph_data()
        return len(deleted)

    def clear_all(self) -> None:
        """清空全部记忆文件 / 切片 / 资源。"""
        service = self._ensure_service()
        with self._io_lock:
            service.database.recall_file_repo.clear_recall_files()
            service.database.recall_file_segment_repo.clear_segments()
            service.database.resource_repo.clear_resources()
        export_graph_data()

    def remember_explicit(self, key: str, value: str) -> None:
        """写入一条显式记忆（name 幂等，归属 AI 自己）。"""
        try:
            _run_sync(self._commit_explicit(key, value))
        except Exception as e:
            console.warn(f"显式记忆写入失败：{e}")

    async def _commit_explicit(self, key: str, value: str) -> None:
        if not self._ensure_embedding():
            return
        service = self._ensure_service()
        with self._io_lock:
            await service.commit_results(
                recall_files=[{
                    "name": (key or "").strip()[:64] or "记忆",
                    "track": "memory",
                    "description": key,
                    "content": str(value or "").strip(),
                }],
                user=self._scope(_USER_SELF),
            )
        export_graph_data()

    def forget_phrase(self, keyword: str) -> int:
        """按关键词删除匹配的记忆（名称 / 描述 / 内容任一包含），返回删除条数。"""
        keyword = (keyword or "").strip().lower()
        if not keyword:
            return 0
        service = self._ensure_service()
        with self._io_lock:
            rows = service.database.recall_file_repo.list_recall_files(
                {"user__in": [_USER_DEFAULT, _USER_SELF]}
            ).values()
            hits = [
                r for r in rows
                if keyword in (r.name or "").lower()
                or keyword in (r.description or "").lower()
                or keyword in (r.content or "").lower()
            ]
            if not hits:
                return 0
            id_list = [r.id for r in hits]
            service.database.recall_file_repo.clear_recall_files({"id__in": id_list})
            service.database.recall_file_segment_repo.clear_segments(
                {"recall_file_id__in": id_list}
            )
        export_graph_data()
        return len(id_list)

    def decay_stale_memories(self) -> int:
        """清理超过 TTL 未更新的记忆，返回清理条数。"""
        service = self._ensure_service()
        with self._io_lock:
            rows = service.database.recall_file_repo.list_recall_files(
                {"user__in": [_USER_DEFAULT, _USER_SELF]}
            ).values()
            cutoff = datetime.now(timezone.utc) - timedelta(days=_MEMORY_TTL_DAYS)
            stale = [
                r for r in rows
                if r.updated_at is not None
                and _to_utc(r.updated_at) < cutoff
            ]
            if not stale:
                return 0
            id_list = [r.id for r in stale]
            service.database.recall_file_repo.clear_recall_files({"id__in": id_list})
            service.database.recall_file_segment_repo.clear_segments(
                {"recall_file_id__in": id_list}
            )
        export_graph_data()
        return len(id_list)

    async def update_archive_async(
        self,
        summary: str,
        period_start: str = "",
        period_end: str = "",
    ) -> None:
        """把会话摘要写入为一条归档记忆（归属 AI 自己，name 幂等）。"""
        summary = (summary or "").strip()
        if not summary:
            return
        if not self._ensure_embedding():
            return
        service = self._ensure_service()
        name = f"会话归档 {period_start or period_end or datetime.now().strftime('%Y-%m-%d %H:%M')}"
        with self._io_lock:
            await service.commit_results(
                recall_files=[{
                    "name": name[:64],
                    "track": "memory",
                    "description": f"会话摘要（{period_start} → {period_end}）",
                    "content": summary,
                }],
                user=self._scope(_USER_SELF),
            )
        export_graph_data()


# ---------- 模块级单例与工具 ----------

_manager: MemoryManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> MemoryManager:
    """全局唯一 MemoryManager（线程安全懒创建）。"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = MemoryManager()
    return _manager


def count() -> int:
    """记忆文件总数（供启动日志等场景）。"""
    return get_manager().count()


def extract_and_strip(text: str) -> str:
    """剥离句子中的 <memory> 标签（TTS 前清洗用，见 llm_brain）。"""
    if not text or "<memory>" not in text:
        return text
    return _MEMORY_TAG_RE.sub("", text)


_MEMORY_TAG_RE = re.compile(r"<memory>.*?</memory>", re.IGNORECASE | re.DOTALL)


async def retrieve(query: str, top_k: int = 8, user: str = _USER_DEFAULT) -> str:
    """检索相关记忆并格式化为 prompt 注入段。

    优先走 memU 向量检索（progressive_retrieve）；embedding 不可用或
    检索异常时回退关键词粗筛（_llm_fallback）。检索耗时打印到控制台。
    """
    if not config.cfg.MEMORY_ENABLED or not (query or "").strip():
        return ""
    manager = get_manager()
    started = time.monotonic()
    try:
        service = manager._ensure_service()
        with manager._io_lock:
            data = await service.progressive_retrieve(
                query,
                where={"user__in": [user, _USER_SELF]},
            )
        elapsed_ms = (time.monotonic() - started) * 1000
        text = _format_retrieval(data)
        hits = len(data.get("segments") or [])
        console.dim(f"[记忆检索] 向量检索 {elapsed_ms:.0f}ms，命中 {hits} 段")
        return text
    except Exception:
        elapsed_ms = (time.monotonic() - started) * 1000
        text = _llm_fallback(query, user)
        console.dim(f"[记忆检索] 向量检索失败，关键词回退耗时 {elapsed_ms:.0f}ms")
        return text


async def warmup() -> None:
    """启动预热：初始化存储（失败仅告警）。"""
    get_manager().load()


async def decay_loop() -> None:
    """后台衰减循环：每 6 小时清理一次过期记忆。"""
    while True:
        try:
            removed = await asyncio.to_thread(decay_stale_memories)
            if removed:
                console.dim(f"记忆衰减：清理 {removed} 条过期记忆")
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


def decay_stale_memories() -> int:
    """模块级入口：清理过期记忆。"""
    return get_manager().decay_stale_memories()


def remember_explicit(key: str, value: str) -> None:
    """模块级入口：写入显式记忆（memory_tools 经 to_thread 调用）。"""
    get_manager().remember_explicit(key, value)


def forget_phrase(keyword: str) -> int:
    """模块级入口：按关键词遗忘记忆。"""
    return get_manager().forget_phrase(keyword)


async def aclose() -> None:
    """关闭存储连接（程序退出时调用）。"""
    manager = get_manager()
    service = manager._service
    if service is not None:
        try:
            service.database.close()
        except Exception:
            pass
        manager._service = None


def export_graph_data() -> str | None:
    """记忆变更后原子导出图谱快照（控制中心跨进程只读数据源）。

    快照格式为 {"files": [...]}，与控制中心 _load_graph_export 的读取契约一致。
    返回快照路径；失败返回 None（仅告警，不影响主流程）。
    """
    try:
        files, _ = get_manager().graph_data(limit=2000)
        path = os.path.join(config.cfg.PROJECT_ROOT, _GRAPH_EXPORT_REL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"files": files}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return path
    except Exception as e:
        console.dim(f"记忆图谱快照导出失败：{e}")
        return None


# ---------- 内部工具 ----------

def _run_sync(coro):
    """在同步上下文执行协程（无运行 loop 直接跑；有则放入线程池）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _to_utc(value) -> datetime:
    """任意时间值归一化为 UTC datetime（memU 记录为 pendulum UTC）。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc)


def _is_local_url(base_url: str) -> bool:
    """本地端点判断（本地 llama.cpp / ollama 免 API Key，与 embedding.py 一致）。"""
    return any(host in (base_url or "") for host in ("127.0.0.1", "localhost", "0.0.0.0"))


def _format_retrieval(data: dict) -> str:
    """把 memU progressive_retrieve 的三层结果格式化为注入文本。

    形状对标 memU hosts/retrieval.py：segments 为命中切片（带 score），
    files 为来源文档 roll-up（同源去重）。
    """
    segments = data.get("segments") or []
    files = data.get("files") or []
    lines: list[str] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        score = seg.get("score")
        score_s = f"{float(score):.2f}" if isinstance(score, (int, float)) else "-"
        lines.append(f"[记忆·命中] ({score_s}) {text}")
    seen: set[str] = set()
    for item in files:
        name = (item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        description = (item.get("description") or "").strip()
        lines.append(f"[记忆·文档] {name}" + (f"：{description}" if description else ""))
    return "\n".join(lines) if lines else ""


def _llm_fallback(query: str, user: str = _USER_DEFAULT) -> str:
    """embedding 不可用时的关键词粗筛回退（无 LLM 调用，语义清晰）。"""
    try:
        files = get_manager().list_files(limit=200)
    except Exception:
        return ""
    keywords = [k for k in re.split(r"[\s,，。？！.!?；;]+", query) if len(k) >= 2]
    candidates: list[dict] = []
    for item in files:
        if (item.get("user") or _USER_DEFAULT) not in (user, _USER_SELF):
            continue
        haystack = f"{item.get('name') or ''} {item.get('content') or ''}".lower()
        if any(k.lower() in haystack for k in keywords):
            candidates.append(item)
    if not candidates:
        return ""
    lines = [f"[记忆] {c.get('name')}：{(c.get('content') or '').strip()}" for c in candidates[:5]]
    return "\n".join(lines)


def format_turns_text(turns: list[dict]) -> str:
    """把对话轮次格式化为角色明确的文本（供 LLM 记忆提取/复盘/进化使用）。

    区分三方发言，避免弹幕（观众）被误当作主播或 AI 自己的发言：
    - AI 发言（role=assistant/muika）→ "AI：..."
    - 弹幕（source=danmaku_input 或内容以 [弹幕@ 开头）→ "观众：..."
    - 其余轮次（主播输入等）→ "主播：..."
    """
    if not turns:
        return ""
    lines = []
    for t in turns:
        content = (t.get("content") or "").strip()
        if not content:
            continue
        role = str(t.get("role") or "").strip().lower()
        if role in ("assistant", "muika"):
            speaker = "AI"
        elif t.get("source") == "danmaku_input" or content.startswith("[弹幕@"):
            speaker = "观众"
        else:
            speaker = "主播"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


__all__ = [
    "MemoryManager",
    "get_manager",
    "count",
    "retrieve",
    "format_turns_text",
    "extract_and_strip",
    "STANDING_INSTRUCTION",
    "remember_explicit",
    "forget_phrase",
    "warmup",
    "decay_loop",
    "decay_stale_memories",
    "aclose",
    "export_graph_data",
    "_USER_SELF",
    "_USER_DEFAULT",
    "_AGENT_ID",
    "_ensure_memu_path",
]
