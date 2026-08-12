"""vtuber 记忆层：基于 memU 引擎（src/memory/memu）的全新封装。

memU 只负责存储与向量检索（三入口：list_all_recall_files /
progressive_retrieve / commit_results，见 memu/AGENTS.md），
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
from collections import OrderedDict
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

# 图谱快照防抖窗口（ms）。写路径在 500ms 内多次触发只导一次：
# - 写密集场景省下 50-200ms / 次（每次都同步全表 + 写 2000 条 JSON）
# - 控制中心 500ms 延迟肉眼无感
_GRAPH_DEBOUNCE_MS = 500

# ---------- memU 引擎路径注入（顶层执行，惰性 import 由使用时触发） ----------

_MEMU_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memu", "src")


def _ensure_memu_path() -> None:
    """把 memU 引擎源码目录加入 sys.path，使顶层 import memu 可用。"""
    if _MEMU_SRC not in sys.path:
        sys.path.insert(0, _MEMU_SRC)


_ensure_memu_path()

from src.utils import config, console  # noqa: E402
from src.utils.constants import (
    ROLE_ASSISTANT, ROLE_AI_ALIAS, SOURCE_DANMAKU_INPUT,
)


# ---------- 进程内加速：内存向量索引 / 文件池 / 嵌入 LRU ----------
#
# 设计动机：memU 的 progressive_retrieve 每次都走 SQL 拉全表 segments
# + JSON 反序列化全部 embedding + 重建 numpy 矩阵。检索是每轮对话必跑
# 的热路径，写入相对稀疏（一次 commit 一批），因此维护一个常驻的进程内
# 索引，把热路径上的 SQL/JSON/numpy 重建全部省掉，写入路径只做一个
# dirty 标记 + 下次检索前 lazy 重建。
#
# 这些类只依赖 numpy（pyproject 已声明），不依赖任何 memU 内部状态；
# 第三方库升级时不会受影响。

_NP = None  # 进程级 numpy 懒加载（避免缺 numpy 环境在 import 期就崩）


def _np():
    global _NP
    if _NP is None:
        import numpy as _numpy  # type: ignore
        _NP = _numpy
    return _NP


class _SegmentIndex:
    """进程内 segment 向量索引。

    把所有 segment 的 embedding 堆叠成单个 ``(N, D) float32`` 矩阵，存
    储时已 L2 归一化（`cosine = a·b`），search 时 query 也归一化后
    做一次矩阵乘 + argpartition topk，没有 Python 循环。

    并发模型：
    - 写路径（mark_dirty / rebuild）持 _lock，互斥；
    - 读路径（search）锁内快照 (matrix, ids, file_ids, users, texts) 后立即
      释放锁，再做矩阵乘等耗时计算，不阻塞其它 retrieve。

    - rebuild(segments, file_user_map): 从 `list_segments()` 重建
    - search(query_vec, k, user_filter) -> [(seg_id, score, file_id, text), ...]
    - mark_dirty() / is_dirty(): 写入路径标脏，下次 retrieve 前重建
    """

    __slots__ = ("_lock", "_dirty", "_matrix", "_ids", "_file_ids", "_users", "_texts")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dirty = True
        self._matrix = None  # np.ndarray | None
        self._ids: list[str] = []
        self._file_ids: list[str] = []
        self._users: list[str] = []
        self._texts: list[str] = []

    def is_dirty(self) -> bool:
        with self._lock:
            return self._dirty

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def is_ready(self) -> bool:
        with self._lock:
            return not self._dirty and self._matrix is not None

    def size(self) -> int:
        with self._lock:
            return 0 if self._matrix is None else int(self._matrix.shape[0])

    def rebuild(self, segments: list, file_user_map: dict[str, str]) -> None:
        """从 `list_segments()` 全表结果重建。

        跳过 embedding 为空、所属 file 在 user map 里查不到、或范数为 0
        的行（与原 memU 逻辑一致：None vec 不参与 topk）。
        """
        ids: list[str] = []
        file_ids: list[str] = []
        users: list[str] = []
        texts: list[str] = []
        vecs = []
        np = _np()
        for seg in segments:
            if not seg.embedding:
                continue
            owner = file_user_map.get(seg.recall_file_id)
            if owner is None:
                continue
            arr = np.asarray(seg.embedding, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if norm == 0.0:
                continue
            vecs.append(arr / norm)
            ids.append(seg.id)
            file_ids.append(seg.recall_file_id)
            users.append(owner)
            texts.append(seg.text or "")
        if vecs:
            new_matrix = np.vstack(vecs).astype(np.float32, copy=False)
        else:
            new_matrix = None
        with self._lock:
            self._matrix = new_matrix
            self._ids = ids
            self._file_ids = file_ids
            self._users = users
            self._texts = texts
            self._dirty = False

    def search(
        self,
        query_vec: list[float],
        k: int,
        user_filter: set[str] | None,
    ) -> list[tuple[str, float, str, str]]:
        """Topk 余弦检索：纯 numpy 矩阵乘 + argpartition。

        返回 [(seg_id, score, file_id, text), ...]，按 score 降序。
        user_filter 非空时按 segment 的 user 字段过滤（按 file 归属）。

        锁内仅做"快照 5 个列表/矩阵引用"的 O(1) 操作，立刻释放；
        锁外的 numpy 计算不阻塞其它 retrieve。
        """
        with self._lock:
            matrix = self._matrix
            ids = self._ids
            file_ids = self._file_ids
            users = self._users
            texts = self._texts
        if k <= 0 or matrix is None or matrix.shape[0] == 0:
            return []
        try:
            np = _np()
            q = np.asarray(query_vec, dtype=np.float32)
            qn = float(np.linalg.norm(q))
            if qn == 0.0:
                return []
            q = q / qn

            n = int(matrix.shape[0])
            if user_filter:
                mask = np.fromiter(
                    (u in user_filter for u in users),
                    dtype=bool,
                    count=n,
                )
            else:
                mask = np.ones(n, dtype=bool)
            if not bool(mask.any()):
                return []
            sub = matrix[mask]
            scores = sub @ q
            actual_k = min(k, int(scores.shape[0]))
            # argpartition O(n) 选 topk，再对 topk 做 sort 拿降序
            top = np.argpartition(-scores, actual_k - 1)[:actual_k]
            top = top[np.argsort(-scores[top])]
            global_idx = np.flatnonzero(mask)
            out: list[tuple[str, float, str, str]] = []
            for i in top:
                gi = int(global_idx[i])
                out.append(
                    (ids[gi], float(scores[i]), file_ids[gi], texts[gi])
                )
            return out
        except Exception:
            return []


class _FilePool:
    """进程内 file 元数据池（检索专用视图，不含 content 大字段）。

    - rebuild(files): 从 `_walk_files()` 重建
    - get(fid): 取单个 file 的最小字段（name/description/user/track）
    - get_contents_for_owner(owner): 拿该 owner 已有的 content 集合（commit 阶段精确去重用）
    - update(file) / remove(fid): 写后增量维护
    - mark_dirty() / is_dirty(): 同步 write→read 一致性
    """

    __slots__ = ("_files", "_content_by_owner", "_dirty")

    def __init__(self) -> None:
        self._files: dict[str, dict] = {}
        self._content_by_owner: dict[str, set[str]] = {}
        self._dirty = True

    def is_dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        self._dirty = True

    def rebuild(self, files: list[dict]) -> None:
        self._files = {}
        self._content_by_owner = {}
        for f in files:
            fid = f.get("id")
            if not fid:
                continue
            owner = f.get("user") or _USER_DEFAULT
            self._files[fid] = {
                "id": fid,
                "name": f.get("name") or "",
                "description": f.get("description") or "",
                "user": owner,
                "track": f.get("track") or "memory",
            }
            content = (f.get("content") or "").strip()
            if content:
                self._content_by_owner.setdefault(owner, set()).add(content)
        self._dirty = False

    def get(self, fid: str) -> dict | None:
        return self._files.get(fid)

    def get_contents_for_owner(self, owner: str) -> set[str]:
        return self._content_by_owner.get(owner, set())

    def update(self, file: dict) -> None:
        fid = file.get("id")
        if not fid:
            return
        owner = file.get("user") or _USER_DEFAULT
        self._files[fid] = {
            "id": fid,
            "name": file.get("name") or "",
            "description": file.get("description") or "",
            "user": owner,
            "track": file.get("track") or "memory",
        }
        content = (file.get("content") or "").strip()
        if content:
            self._content_by_owner.setdefault(owner, set()).add(content)

    def remove(self, fid: str) -> None:
        f = self._files.pop(fid, None)
        if f:
            # 找不到 fid → 该 file 的 content 也一并放弃；保守起见标脏，
            # 下次 commit / retrieve 前重建一次，杜绝残留。
            self._dirty = True


class _QueryEmbedCache:
    """query 嵌入 LRU：相同文本短时间内重复时跳过 API 调用。

    对话循环里 "哈哈哈"、"好的"、"我今天..." 等模板化输入常重复；用
    256 容量足以覆盖一个会话的窗口。线程安全（_io_lock 同一时刻只有
    一个 retrieve 跑建索引/缓存路径）。
    """

    __slots__ = ("_data", "_cap")

    def __init__(self, capacity: int = 256) -> None:
        self._data: OrderedDict[str, list[float]] = OrderedDict()
        self._cap = capacity

    def get(self, text: str) -> list[float] | None:
        v = self._data.get(text)
        if v is not None:
            self._data.move_to_end(text)
        return v

    def put(self, text: str, vec: list[float]) -> None:
        self._data[text] = vec
        self._data.move_to_end(text)
        while len(self._data) > self._cap:
            self._data.popitem(last=False)


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
        # 进程内加速：内存向量索引 / 文件池 / query 嵌入 LRU。
        # 写路径只标脏，读路径在 dirty 时 lazy 重建；快路径不持 _io_lock，
        # 多 retrieve 可并发（SQLite WAL 自带读不阻塞写）。
        self._index = _SegmentIndex()
        self._files = _FilePool()
        self._query_cache = _QueryEmbedCache(capacity=256)

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
            ProgressiveRetrieveConfig,
            RetrieveFileConfig,
            RetrieveResourceConfig,
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
            # 检索配置（vtuber 调优）：
            # - resource.enabled=False: vtuber 路径 commit_results 只传
            #   recall_files 不传 resource，resource 表永远空——关闭
            #   _recall_resources 每轮都空跑的浪费（20-50ms / 千条）
            # - file.top_k=8: 与 retrieve() 默认 top_k 对齐，慢路径兜底
            #   时也能给到 8 段而不是 memU 默认的 5 段
            progressive_retrieve_config=ProgressiveRetrieveConfig(
                file=RetrieveFileConfig(enabled=True, top_k=8),
                resource=RetrieveResourceConfig(enabled=False),
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
        """启动时初始化存储（失败仅告警，记忆功能降级为不可用）。

        成功时同步建好进程内 segment 索引 + file 池——首次 retrieve 即
        命中快路径，不需要 lazy 重建带来的 50ms 抖动。
        """
        try:
            self._ensure_service()
        except Exception as e:
            console.warn(f"记忆系统不可用（本次会话无长期记忆）：{e}")
            return
        try:
            self._build_index()
            if self._index.is_ready():
                console.dim(
                    f"[记忆] 启动预热完成：{self._index.size()} 段向量已加载"
                )
        except Exception as e:
            console.dim(f"[记忆] 索引预热失败（将回退慢路径）：{e}")

    def _build_index(self) -> None:
        """(Re)build in-memory segment vector index and file pool from SQLite.

        - 读 SQLite 不持 `_io_lock`：SQLite WAL 模式下读与单写可并发
          （busy_timeout 兜底），把检索从全局串行中解放出来
        - 写后第一次 retrieve 触发 lazy 重建：写路径只 mark_dirty()，
          不重算；O(1) 写代价换 O(N) 重建只发生在检索前
        - 失败保持 dirty 状态：下次重试或回退 memU 慢路径
        """
        try:
            files = self._walk_files()
        except Exception:
            return
        self._files.rebuild(files)
        file_user_map = {
            f["id"]: f.get("user") or _USER_DEFAULT for f in files if f.get("id")
        }
        try:
            service = self._ensure_service()
            segments = service.database.recall_file_segment_repo.list_segments()
        except Exception:
            return
        self._index.rebuild(segments, file_user_map)

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
            if str(role).strip().lower() in (ROLE_ASSISTANT, ROLE_AI_ALIAS)
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
                    # yield_per 分批流式读取：避免记忆库增长后全表实例化进内存
                    for row in session.exec(select(model)).yield_per(1000):
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

    # ---------- 检索（快/慢路径） ----------

    async def _retrieve_fast(
        self, query: str, top_k: int, user: str, started: float
    ) -> str:
        """快路径：内存向量索引 + LRU 嵌入缓存。

        流程：
          1) 拿 query 嵌入（LRU 命中跳过 API 调用）
          2) 在已预归一化的 segment 矩阵里按 user 过滤 + 矩阵乘 + topk
          3) 用内存 file 池做 rollup（不查 SQL）

        整个流程无 SQL、无 `_io_lock`、无 list_comprehension 重建向量。
        """
        # 1) embed query（LRU 优先；同 query 重复时直接命中）
        qvec = self._query_cache.get(query)
        if qvec is None:
            try:
                service = self._ensure_service()
                embed_client = service._get_embedding_client("embedding")
                vectors, _ = await embed_client.embed([query])
                qvec = list(vectors[0])
            except Exception:
                return await self._retrieve_slow(query, top_k, user, started)
            self._query_cache.put(query, qvec)

        # 2) topk
        user_filter = {user, _USER_SELF}
        hits = self._index.search(qvec, top_k, user_filter)
        if not hits:
            elapsed_ms = (time.monotonic() - started) * 1000
            console.dim(f"[记忆检索] 快路径 {elapsed_ms:.0f}ms，命中 0 段")
            return ""

        # 3) 构造输出：segments 按 score 降序，files 按 max-score 降序
        seg_lines: list[tuple[float, str]] = []
        file_max_score: dict[str, float] = {}
        for _seg_id, score, file_id, text in hits:
            t = (text or "").strip()
            if t:
                seg_lines.append((score, t))
            cur = file_max_score.get(file_id, -1.0)
            if score > cur:
                file_max_score[file_id] = score

        seg_lines.sort(key=lambda x: -x[0])
        segments_out = [{"text": t, "score": s} for s, t in seg_lines]

        file_ids_sorted = sorted(file_max_score, key=lambda fid: -file_max_score[fid])
        files_out: list[dict] = []
        seen_names: set[str] = set()
        for fid in file_ids_sorted:
            f = self._files.get(fid)
            if not f:
                continue
            name = f.get("name") or ""
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            files_out.append({"name": name, "description": f.get("description") or ""})

        text = _format_retrieval({"segments": segments_out, "files": files_out})
        elapsed_ms = (time.monotonic() - started) * 1000
        console.dim(f"[记忆检索] 快路径 {elapsed_ms:.0f}ms，命中 {len(segments_out)} 段")
        return text

    async def _retrieve_slow(
        self, query: str, top_k: int, user: str, started: float
    ) -> str:
        """慢路径：原 memU progressive_retrieve（保留兼容 + 异常兜底）。"""
        try:
            service = self._ensure_service()
            with self._io_lock:
                data = await service.progressive_retrieve(
                    query, where={"user__in": [user, _USER_SELF]}
                )
            elapsed_ms = (time.monotonic() - started) * 1000
            text = _format_retrieval(data)
            hits = len(data.get("segments") or [])
            console.dim(f"[记忆检索] 慢路径 {elapsed_ms:.0f}ms，命中 {hits} 段")
            return text
        except Exception:
            elapsed_ms = (time.monotonic() - started) * 1000
            text = _llm_fallback(query, user)
            console.dim(f"[记忆检索] 慢路径失败，关键词回退 {elapsed_ms:.0f}ms")
            return text

    # ---------- 写入 ----------

    async def commit_recall_files(self, files: list[dict]) -> dict:
        """按归属分组写入记忆文件（memU commit_results，name 幂等）。

        写入前两级去重，避免重复提取污染检索：
        1. 精确去重：同用户已存在相同内容的记忆跳过（用 file_pool 替代
           原 `_walk_files()` 全表扫——O(1) 内存查）；
        2. 语义去重：与同用户已有记忆向量相似度 ≥ 阈值（近似重复）跳过
           （改走内存索引，不再调 memU `progressive_retrieve`）。

        写后标 dirty，索引在下次 retrieve 前 lazy 重建。
        """
        if not self._ensure_embedding():
            return {"recall_files": []}
        service = self._ensure_service()
        with self._io_lock:
            # 已有内容集合（精确去重基准）：user → {content}
            # 用内存 file_pool：dirty 时一次拉表，不 dirty 直接 O(1) 读
            if self._files.is_dirty():
                try:
                    self._files.rebuild(self._walk_files())
                except Exception:
                    pass
            existing: dict[str, set[str]] = {
                owner: set(contents)
                for owner, contents in self._files._content_by_owner.items()
            }
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
                # file_pool 同步增：新提交的文件已写入 SQLite
                for c in result.get("recall_files") or []:
                    self._files.update({
                        "id": c.get("id"),
                        "name": c.get("name") or "",
                        "description": c.get("description") or "",
                        "user": owner,
                        "track": c.get("track") or "memory",
                        "content": "",  # 内容由 segment 覆盖，pool 不存 content
                    })
        # 写后标脏（segment 表由 memU 内部管理，下次 retrieve 重建）
        self._index.mark_dirty()
        export_graph_data()
        return {"recall_files": committed}

    async def _semantic_duplicate(self, content: str, owner: str) -> bool:
        """语义去重：与同归属者已有记忆高度相似（余弦 ≥ 阈值）视为重复。

        改用内存 segment 索引做 top1 检索（之前走 memU progressive_retrieve
        + `_io_lock`，写入路径的瓶颈之一）。索引未就绪时回退慢路径。
        """
        if self._index.is_dirty():
            try:
                self._build_index()
            except Exception:
                return False
        if not self._index.is_ready() or self._index.size() == 0:
            return False
        qvec = self._query_cache.get(content)
        if qvec is None:
            try:
                service = self._ensure_service()
                embed_client = service._get_embedding_client("embedding")
                vectors, _ = await embed_client.embed([content])
                qvec = list(vectors[0])
            except Exception:
                return False
            self._query_cache.put(content, qvec)
        hits = self._index.search(qvec, 1, {owner})
        if not hits:
            return False
        return hits[0][1] >= _SEMANTIC_DUP_THRESHOLD

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
        for fid in id_list:
            self._files.remove(fid)
        self._index.mark_dirty()
        export_graph_data()
        return len(deleted)

    def clear_all(self) -> None:
        """清空全部记忆文件 / 切片 / 资源。"""
        service = self._ensure_service()
        with self._io_lock:
            service.database.recall_file_repo.clear_recall_files()
            service.database.recall_file_segment_repo.clear_segments()
            service.database.resource_repo.clear_resources()
        self._files._files = {}
        self._files._content_by_owner = {}
        # 持 _index._lock 串行写，避免与 search() 锁内快照读到的旧引用冲突
        with self._index._lock:
            self._index._matrix = None
            self._index._ids = self._index._file_ids = self._index._users = self._index._texts = []
            self._index._dirty = False
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
        self._index.mark_dirty()
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
        for fid in id_list:
            self._files.remove(fid)
        self._index.mark_dirty()
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
        for fid in id_list:
            self._files.remove(fid)
        self._index.mark_dirty()
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
        self._index.mark_dirty()
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

    快路径（默认）：进程内 segment 向量索引 + LRU 嵌入缓存，单次矩阵乘
    完成 topk。**不持 `_io_lock`，不查 SQL**，每轮对话都能命中。

    慢路径（兜底）：embedding 未配置、索引未就绪、或快路径异常时回退到
    memU `progressive_retrieve`；仍异常时再回退到关键词粗筛。两条路径
    行为对调用方等价。
    """
    if not config.cfg.MEMORY_ENABLED or not (query or "").strip():
        return ""
    manager = get_manager()
    started = time.monotonic()

    # 1) embedding 不可用 → 关键词回退（与原行为一致）
    if not manager.embedding_enabled:
        text = _llm_fallback(query, user)
        elapsed_ms = (time.monotonic() - started) * 1000
        console.dim(f"[记忆检索] 无 embedding，关键词回退 {elapsed_ms:.0f}ms")
        return text

    # 2) 索引 dirty（写后第一次读）→ 重建。重建只在 SQLite 拉全表 + numpy
    #    堆叠，通常 <50ms / 万段；命中快路径后的稳态不再触发
    if manager._index.is_dirty() or manager._files.is_dirty():
        manager._build_index()

    # 3) 快路径
    if manager._index.is_ready() and manager._index.size() > 0:
        return await manager._retrieve_fast(query, top_k, user, started)

    # 4) 慢路径（兜底，与原行为一致）
    return await manager._retrieve_slow(query, top_k, user, started)


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
    # 落盘最后一次图谱快照（debounce 窗口可能还没到，必须强制导出）
    try:
        _flush_graph_export()
    except Exception:
        pass
    manager = get_manager()
    service = manager._service
    if service is not None:
        try:
            service.database.close()
        except Exception:
            pass
        manager._service = None


def export_graph_data() -> str | None:
    """记忆变更后调度一次图谱快照导出（debounce + 后台线程）。

    写路径（commit / delete / decay / forget 等）可能短时间内连续触发；
    每次同步全表 + 写 2000 条 JSON 在写密集场景累加明显。改为：
    - 500ms 窗口内合并多次触发（debounce）
    - 后台 daemon 线程执行（写路径零等待）
    - 控制中心（UI）只读快照，500ms 延迟肉眼无感
    - 程序退出时 `_flush_graph_export()` 同步落盘最后状态
    """
    _ensure_graph_thread()
    _graph_event.set()
    return None


# ---------- 图谱快照防抖：内部状态 ----------

_graph_event = threading.Event()
_graph_lock = threading.Lock()
_graph_thread_started = False
_graph_shutdown = False


def _do_export_graph() -> str | None:
    """实际导出图谱快照（debounce worker 与 flush 共用）。"""
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


def _graph_export_worker() -> None:
    """后台线程：等 debounce 窗口过去后做一次 export，然后继续等下一次。"""
    while not _graph_shutdown:
        _graph_event.wait()
        if _graph_shutdown:
            return
        _graph_event.clear()
        time.sleep(_GRAPH_DEBOUNCE_MS / 1000.0)
        if _graph_shutdown:
            return
        # 窗口内有新触发 → 让下一轮继续等；无 → 导出
        if _graph_event.is_set():
            continue
        _do_export_graph()


def _ensure_graph_thread() -> None:
    global _graph_thread_started
    if _graph_thread_started:
        return
    with _graph_lock:
        if _graph_thread_started:
            return
        t = threading.Thread(
            target=_graph_export_worker,
            name="mem-graph-export",
            daemon=True,
        )
        t.start()
        _graph_thread_started = True


def _flush_graph_export() -> str | None:
    """同步强制导出（程序退出时确保最后一次变更落盘）。

    同时让后台 worker 退出（`aclose` 之后不会再有写触发）。如果期间
    worker 刚好也导了一次，结果是同一份 JSON 的二次写盘，无副作用。
    """
    global _graph_shutdown
    if not _graph_event.is_set():
        return None
    _graph_event.clear()
    _graph_shutdown = True
    _graph_event.set()  # 唤醒 worker 让其看到 shutdown 标志并退出
    return _do_export_graph()


# ---------- 内部工具 ----------

def _run_sync(coro):
    """在同步上下文执行协程。

    关键：永远不要在线程里新建 event_loop 后 run_until_complete 一个会
    跨线程通讯的协程（行为不可预期）。已有 loop 时丢到默认线程池，
    由 worker 线程里的 asyncio.run 跑。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行 loop → 直接 run（仅主线程同步工具调用路径）
        return asyncio.run(coro)
    # 已有 loop：把协程丢到默认 executor
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result(timeout=60)


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
        if role in (ROLE_ASSISTANT, ROLE_AI_ALIAS):
            speaker = "AI"
        elif t.get("source") == SOURCE_DANMAKU_INPUT or content.startswith("[弹幕@"):
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
