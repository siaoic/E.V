"""memu_adapter：vtuber 配置 → memU MemoryService 配置桥接。

把 vtuber 的 EMBEDDING_* / PROJECT_ROOT 等配置转换成 memU 的
EmbeddingProfilesConfig / DatabaseConfig / ProgressiveRetrieveConfig /
UserConfig，并提供工厂函数 create_memu_service() 返回开箱即用的
memU MemoryService 实例。

适配要点：
  - embedding 走 SiliconFlow（OpenAI 兼容协议），复用 memU 的 sdk backend
    （OpenAIEmbeddingSDKClient + AsyncOpenAI）；
  - 存储走 sqlite（data/memu.sqlite3 绝对路径）；
  - user_model 在 memU DefaultUserModel 基础上扩展 user 字段，兼容 vtuber
    的记忆归属概念（chao = 本机用户，self = AI 自己，对齐 memU ADR 0003）；
  - 返回的 MemoryService 即暴露 memU 三入口（list_all_recall_files /
    progressive_retrieve / commit_results），vtuber 旧 API 可直接对接。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Optional

# 确保 src/memory/ 目录在 sys.path 上，使 memU 能以顶层包 `memu` 导入
# （memU 内部所有 import 都是 `from memu.xxx import yyy`，不依赖包前缀；
#   memu 包本体位于 src/memory/memu/，与适配器同目录）
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from pydantic import BaseModel, Field

from src.utils import config
from src.utils import console

from memu.app.service import MemoryService
from memu.app.settings import (
    DatabaseConfig,
    DefaultUserModel,
    EmbeddingConfig,
    EmbeddingProfilesConfig,
    MetadataStoreConfig,
    ProgressiveRetrieveConfig,
    UserConfig,
)

# vtuber 用户归属常量（与 src/memory/memory.py _USER_SELF / _USER_DEFAULT 一致）：
# AI 自己的记忆固定 self，用户输入提取的记忆归当前用户名（默认 chao）。
USER_SELF = "self"
USER_DEFAULT = os.getenv("MEMORY_USER_DEFAULT", "chao")

# SiliconFlow embedding 批量大小（云端支持批量，32 为保守值避免限流，
# 与 src/llm/embedding.py _DEFAULT_BATCH 一致）。
_EMBED_BATCH_SIZE = 32


class VtuberUserModel(DefaultUserModel):
    """vtuber 用户模型：兼容 memU DefaultUserModel 并扩展 user 归属字段。

    user_id / agent_id 与 memU 一致（多 agent / 多 session 隔离）；
    user 字段承载 vtuber 的记忆归属概念（chao = 本机用户 / self = AI 自己），
    对齐 src/memory/memory.py 的 _USER_DEFAULT / _USER_SELF，使 memU 的
    _normalize_where 能按 user scope 过滤记忆。
    """

    user: Optional[str] = Field(
        default=None,
        description="记忆归属者：chao（本机用户）/ self（AI 自己），对齐 memU ADR 0003 user scope。",
    )


def build_embedding_profiles() -> EmbeddingProfilesConfig:
    """从 vtuber config 构建 memU EmbeddingProfilesConfig。

    SiliconFlow 是 OpenAI 兼容协议，直接复用 memU 的 sdk backend
    （OpenAIEmbeddingSDKClient + AsyncOpenAI）。显式设置 base_url /
    api_key / embed_model，绕开 EmbeddingConfig 的 OpenAI 默认值回填。
    """
    cfg = config.cfg
    embedding_cfg = EmbeddingConfig(
        provider="openai",
        base_url=cfg.EMBEDDING_BASE_URL or "https://api.siliconflow.cn/v1",
        api_key=cfg.EMBEDDING_API_KEY or "",
        embed_model=cfg.EMBEDDING_MODEL or "Qwen/Qwen3-Embedding-0.6B",
        embed_batch_size=_EMBED_BATCH_SIZE,
        embed_dimensions=cfg.EMBEDDING_DIMENSIONS,
        client_backend="sdk",
    )
    # EmbeddingProfilesConfig.ensure_default 会自动补 "embedding" 别名指向 "default"。
    return EmbeddingProfilesConfig({"default": embedding_cfg})


def build_database_config() -> DatabaseConfig:
    """构建 sqlite DatabaseConfig（data/memu.sqlite3 绝对路径）。

    vector_index 未显式指定时，DatabaseConfig.model_post_init 会按
    metadata_store.provider 自动选 bruteforce（sqlite 非 postgres），无需手填。
    """
    cfg = config.cfg
    db_path = os.path.join(cfg.PROJECT_ROOT, "data", "memu.sqlite3")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # sqlite dsn：三斜杠 + 绝对路径（统一正斜杠，跨平台一致）
    dsn = "sqlite:///" + db_path.replace("\\", "/")
    return DatabaseConfig(
        metadata_store=MetadataStoreConfig(
            provider="sqlite",
            ddl_mode="create",
            dsn=dsn,
        ),
    )


def build_progressive_retrieve_config() -> ProgressiveRetrieveConfig:
    """构建 ProgressiveRetrieveConfig（memU 三入口之一 progressive_retrieve 的检索参数）。

    file / resource 两层渐进检索均启用；top_k 取 memU 默认（5），调用方可在
    渐进检索结果上自行截断。
    """
    return ProgressiveRetrieveConfig()


def build_user_config() -> UserConfig:
    """构建 UserConfig（VtuberUserModel，兼容 chao/self 归属）。"""
    return UserConfig(model=VtuberUserModel)


def create_memu_service() -> MemoryService:
    """工厂函数：返回开箱即用的 memU MemoryService 实例。

    组装 embedding profiles（SiliconFlow sdk backend）+ sqlite 数据库 +
    VtuberUserModel，得到的 service 直接暴露 memU 三入口：
      - list_all_recall_files(where, *, cursor, limit)
      - progressive_retrieve(query, where)
      - commit_results(*, recall_files, resource, user)
    vtuber 旧 API 可在此基础上对接（如把记忆写入映射到 commit_results，
    检索映射到 progressive_retrieve）。
    """
    return MemoryService(
        database_config=build_database_config(),
        embedding_profiles=build_embedding_profiles(),
        progressive_retrieve_config=build_progressive_retrieve_config(),
        user_config=build_user_config(),
    )


# segment / file 全量加载缓存 TTL（秒）。
# 每次 progressive_retrieve 都会全量加载所有 segments（~23MB JSON）和 files
# （~22MB JSON）的 embedding 字段并反序列化为 list[float]，是检索延迟的主要
# 来源（~1s）。按 where 参数缓存加载结果，30s 内重复检索直接复用；写操作
# （commit / delete / clear / update）主动失效，保证「写入后立即检索」可见。
_REPO_CACHE_TTL = 30.0


def inject_repo_cache(service: MemoryService) -> MemoryService:
    """缓存 segment / file 全量加载结果，避免每次检索都反序列化 ~45MB JSON。

    memU progressive_retrieve 的 _recall_segments / _collect_files 会调用
    store.recall_file_segment_repo.list_segments(where) 和
    store.recall_file_repo.list_recall_files(where) 全量加载所有记录（含
    embedding JSON 列），每次都重复 SQLite 读取 + JSON 反序列化 + float
    转换（1040 segments × 1024 维 ≈ 23MB，耗时 ~1s）。

    本函数包装这两个 list 方法，按 where 参数做 TTL 缓存（_REPO_CACHE_TTL
    秒），并在所有写路径（commit_results / create_segment /
    delete_segment / delete_segments_for_file / clear_segments /
    get_or_create_recall_file / update_recall_file / clear_recall_files）
    主动失效。返回浅拷贝，避免调用方修改污染缓存。

    静默失败——缓存只是优化，不应阻断主路径。
    """
    try:
        store = service._get_database()
        seg_repo = store.recall_file_segment_repo
        file_repo = store.recall_file_repo
    except Exception:
        return service

    # key: where 规范化元组 → (expire_at, result)
    _seg_cache: dict[tuple, tuple[float, list]] = {}
    _file_cache: dict[tuple, tuple[float, dict]] = {}

    def _where_key(where: Any) -> tuple:
        if not where:
            return ()
        # where 的 value 可能是 list（user_id__in）或标量，统一转 str 参与 key
        return tuple(sorted((str(k), str(v)) for k, v in where.items()))

    def _invalidate_all() -> None:
        _seg_cache.clear()
        _file_cache.clear()

    # ---- 缓存读：list_segments / list_recall_files ----
    if not getattr(seg_repo, "_vtuber_repo_cache_injected", False):
        original_list_segments = seg_repo.list_segments

        def _cached_list_segments(where=None):
            key = _where_key(where)
            now = time.perf_counter()
            hit = _seg_cache.get(key)
            if hit is not None and now < hit[0]:
                return list(hit[1])  # 浅拷贝：调用方可能 append/排序，不污染缓存
            result = original_list_segments(where)
            _seg_cache[key] = (now + _REPO_CACHE_TTL, result)
            return list(result)

        seg_repo.list_segments = _cached_list_segments  # type: ignore[method-assign]
        seg_repo._vtuber_repo_cache_injected = True

    if not getattr(file_repo, "_vtuber_repo_cache_injected", False):
        original_list_files = file_repo.list_recall_files

        def _cached_list_files(where=None):
            key = _where_key(where)
            now = time.perf_counter()
            hit = _file_cache.get(key)
            if hit is not None and now < hit[0]:
                return dict(hit[1])  # 浅拷贝
            result = original_list_files(where)
            _file_cache[key] = (now + _REPO_CACHE_TTL, result)
            return dict(result)

        file_repo.list_recall_files = _cached_list_files  # type: ignore[method-assign]
        file_repo._vtuber_repo_cache_injected = True

    # ---- 写操作失效：segment repo 写方法 ----
    for _method_name in (
        "create_segment", "delete_segment",
        "delete_segments_for_file", "clear_segments",
    ):
        if getattr(seg_repo, f"_vtuber_invalidate_{_method_name}", False):
            continue
        _original = getattr(seg_repo, _method_name, None)
        if _original is None:
            continue

        def _make_invalidating(orig):
            def _wrapper(*args, **kwargs):
                _invalidate_all()
                return orig(*args, **kwargs)
            return _wrapper

        setattr(seg_repo, _method_name, _make_invalidating(_original))
        setattr(seg_repo, f"_vtuber_invalidate_{_method_name}", True)

    # ---- 写操作失效：file repo 写方法 ----
    for _method_name in (
        "get_or_create_recall_file", "update_recall_file", "clear_recall_files",
    ):
        if getattr(file_repo, f"_vtuber_invalidate_{_method_name}", False):
            continue
        _original = getattr(file_repo, _method_name, None)
        if _original is None:
            continue

        def _make_invalidating(orig):
            def _wrapper(*args, **kwargs):
                _invalidate_all()
                return orig(*args, **kwargs)
            return _wrapper

        setattr(file_repo, _method_name, _make_invalidating(_original))
        setattr(file_repo, f"_vtuber_invalidate_{_method_name}", True)

    # ---- 写操作失效：service.commit_results（async）----
    if not getattr(service, "_vtuber_commit_invalidate", False):
        original_commit = service.commit_results

        async def _invalidating_commit(*args, **kwargs):
            _invalidate_all()
            return await original_commit(*args, **kwargs)

        service.commit_results = _invalidating_commit  # type: ignore[method-assign]
        service._vtuber_commit_invalidate = True

    return service


def inject_embedding_cache(service: MemoryService) -> MemoryService:
    """把 vtuber 的 LRU embedding 缓存注入到 memU service 的 embedding client。

    vtuber 的 src/llm/embedding.py 维护了一个模块级 LRU embedding 缓存
    （_EMBED_CACHE，4096 条），记忆检索查询嵌入由此从 ~200ms 降到 ~0ms。
    本函数包装 memU service 的 embedding client.embed，使其优先查该缓存，
    未命中再调原始 SDK 并回填缓存（确定性函数，缓存无时效问题）。

    实现方式：替换 service._get_embedding_client，对返回的 client 包装其
    embed 方法（带幂等标志防重复包装）。静默失败——缓存只是优化，不应阻断主路径。
    """
    try:
        from src.llm.embedding import (
            _embed_cache_get, _embed_cache_put, _embed_single_dedup,
        )
    except Exception:
        return service

    original_get = service._get_embedding_client

    def _cached_get(profile: Optional[str] = None) -> Any:
        client = original_get(profile)
        if getattr(client, "_vtuber_cache_injected", False):
            return client
        original_embed = client.embed

        async def _cached_embed(inputs: list[str]) -> tuple[list[list[float]], Any]:
            # 先查缓存，命中直接复用（按原顺序占位，未命中的收集起来批量请求）
            results: list[Optional[list[float]]] = [None] * len(inputs)
            missing_idx: list[int] = []
            for i, text in enumerate(inputs):
                key = (text or "").strip()
                if key:
                    vec = _embed_cache_get(key)
                    if vec is not None:
                        results[i] = vec
                        continue
                missing_idx.append(i)
            # 未命中的调原始 SDK，结果回填缓存
            if missing_idx:
                missing_texts = [inputs[i] for i in missing_idx]
                _t0 = time.perf_counter()
                if len(missing_idx) == 1:
                    # 单条：走 vtuber 单飞去重——与情绪分类并发嵌入同一段
                    # 文本时只推理一次（复用方直接等发起方结果），减少本地
                    # CPU 推理；发起方失败时复用方重试一次，等价独立调用。
                    async def _original_embed_one(t: str) -> list[float]:
                        vectors, _ = await original_embed([t])
                        return vectors[0]
                    vec = await _embed_single_dedup(
                        missing_texts[0], _original_embed_one)
                    vectors = [vec]
                else:
                    vectors, _response = await original_embed(missing_texts)
                _ms = (time.perf_counter() - _t0) * 1000
                for idx, vec in zip(missing_idx, vectors):
                    key = (inputs[idx] or "").strip()
                    if key:
                        _embed_cache_put(key, vec)
                    results[idx] = vec
                from src.utils import console
                console.info(
                    f"[Embed·记忆] 批量 {len(missing_idx)} 条 → 网络 {_ms:.1f} ms")
            all_vecs = [v if v is not None else [] for v in results]
            return all_vecs, None

        client.embed = _cached_embed
        client._vtuber_cache_injected = True
        return client

    service._get_embedding_client = _cached_get  # type: ignore[method-assign]
    return service
