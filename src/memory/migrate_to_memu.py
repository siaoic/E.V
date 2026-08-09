"""迁移脚本：旧 ChromaDB 单层记忆 → memU 三层模型（RecallFile → Segment → Resource）。

读取 data/chroma/ 下 collection "vtuber_memory" 的旧记忆，按 (user, name)
分组合并成 RecallFile，调用 memU MemoryService.commit_results 写入。

幂等设计：commit_results 按 (name, track, user scope) 做 create-or-update，
segment 按 content diff 增删——重复运行不会产生重复数据，只会覆盖更新。

用法：
    runtime\\Scripts\\python.exe -m src.memory.migrate_to_memu
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from src.memory.memory import _AGENT_ID, _USER_DEFAULT, _create_service
from src.utils import config

# 旧 ChromaDB 持久化目录与 collection 名
_CHROMA_DIR = os.path.join(config.cfg.PROJECT_ROOT, "data", "chroma")
_COLLECTION_NAME = "vtuber_memory"


def _results_to_dict(results: Any) -> dict:
    """把 chromadb 返回值统一成 dict（兼容 0.x dict / 1.x pydantic GetResult）。"""
    if isinstance(results, dict):
        return results
    # pydantic v2 模型
    if hasattr(results, "model_dump"):
        return results.model_dump()
    # 退而求其次：按常见字段名反射
    out: Dict[str, Any] = {}
    for key in ("ids", "embeddings", "metadatas", "documents", "data", "uris"):
        if hasattr(results, key):
            out[key] = getattr(results, key)
    return out


def _read_old_memories() -> Optional[List[dict]]:
    """读取旧 ChromaDB 记忆。

    返回记录列表（每条 {id, content, name, description, track, user, created_at}）；
    目录 / collection 不存在时返回 None（表示全新安装，无旧数据）。
    兼容 chromadb 0.x（documents）与 1.x（data）字段命名，以及 metadata 缺失情况。
    """
    # 目录不存在 = 全新安装，无旧数据
    if not os.path.isdir(_CHROMA_DIR):
        return None
    # 目录存在但无 sqlite 文件 = 残留空目录，也视为无数据
    if not os.path.exists(os.path.join(_CHROMA_DIR, "chroma.sqlite3")):
        return None

    import chromadb
    client = chromadb.PersistentClient(path=_CHROMA_DIR)
    # collection 不存在视为无数据（get_collection 在 1.x 抛 ValueError）
    try:
        collection = client.get_collection(_COLLECTION_NAME)
    except Exception:
        return None

    # 默认不含 embeddings，只取 ids / metadatas / 文档
    results = collection.get()
    rd = _results_to_dict(results)
    ids = rd.get("ids") or []
    metadatas = rd.get("metadatas") or []
    # 1.x 用 data，0.x 用 documents
    documents = rd.get("data") or rd.get("documents") or []

    records: List[dict] = []
    for i, rid in enumerate(ids):
        # metadata 可能为 None（旧记录无 payload 时）
        meta = metadatas[i] if i < len(metadatas) else None
        if not isinstance(meta, dict):
            meta = {}
        # content：优先 chromadb 文档字段，回退 metadata 里的 data 键
        content = ""
        if i < len(documents):
            content = documents[i] or ""
        if not content:
            content = meta.get("data") or ""
        records.append({
            "id": rid,
            "content": str(content) if content is not None else "",
            "name": meta.get("name"),
            "description": meta.get("description"),
            "track": meta.get("track"),
            "user": meta.get("user"),
            "created_at": meta.get("created_at"),
        })
    return records


def _group_records(records: List[dict]) -> List[dict]:
    """按 (user, name) 分组合并成 RecallFile 草稿列表。

    同组的 content 用空行拼接成文件正文；name/track/description 取组内首条非空值。
    缺省值：user → "chao"，track → "memory"。无 name 的记录无法按 name 去重，跳过。
    """
    # 组内元信息：首条非空值优先
    drafts: Dict[Tuple[str, str], dict] = {}
    # 组内 content 收集（保持顺序）
    contents: Dict[Tuple[str, str], List[str]] = defaultdict(list)

    for r in records:
        name = str(r.get("name") or "").strip()
        if not name:
            # 无 name 无法按 name 去重（commit_results 的 create-or-update 依赖 name）
            continue
        user = str(r.get("user") or _USER_DEFAULT).strip()[:32] or _USER_DEFAULT
        track = str(r.get("track") or "memory").strip() or "memory"
        content = str(r.get("content") or "").strip()
        description = str(r.get("description") or "").strip()

        key = (user, name)
        contents[key].append(content)
        if key not in drafts:
            drafts[key] = {
                "name": name,
                "track": track,
                "description": description,
                "user": user,
            }
        else:
            # 当前草稿对应字段为空时，用后续记录补上
            if not drafts[key]["track"] and track:
                drafts[key]["track"] = track
            if not drafts[key]["description"] and description:
                drafts[key]["description"] = description

    # 合并组内 content 成文件正文
    files: List[dict] = []
    for key, draft in drafts.items():
        parts = [c for c in contents[key] if c]
        draft["content"] = "\n\n".join(parts)
        files.append(draft)
    return files


async def migrate() -> None:
    """主迁移流程：读取旧数据 → 按 (user, name) 分组 → 调 memU commit_results 写入。"""
    records = _read_old_memories()
    if not records:
        print("无旧数据，跳过迁移")
        return

    print(f"读取到 {len(records)} 条旧记忆")
    files = _group_records(records)
    # 过滤掉内容为空的组（commit_results 对空 content 无意义）
    files = [f for f in files if f.get("content")]
    print(f"分组成 {len(files)} 个 RecallFile")

    if not files:
        print("无有效内容可迁移，跳过")
        return

    # 创建 memU MemoryService（经 memu_adapter 装配 + 注入 embedding 缓存）
    service = _create_service()

    # 按 user 分组提交：同用户的文件一次 commit_results，memU 内部按 name 去重更新
    by_user: Dict[str, List[dict]] = defaultdict(list)
    for f in files:
        by_user[f["user"]].append({
            "name": f["name"],
            "track": f["track"],
            "description": f["description"],
            "content": f["content"],
        })

    ok = 0
    failed = 0
    for user, user_files in by_user.items():
        try:
            await service.commit_results(
                recall_files=user_files,
                user={
                    "user_id": user,
                    "agent_id": _AGENT_ID,
                    "user": user,
                },
            )
            ok += len(user_files)
            print(f"  user={user}: 写入 {len(user_files)} 个文件")
        except Exception as e:
            failed += len(user_files)
            print(f"  user={user}: 写入失败（{len(user_files)} 个）"
                  f"{type(e).__name__}: {e}")

    print(
        f"\n迁移完成：读取 {len(records)} 条，分组成 {len(files)} 个文件，"
        f"写入成功 {ok} 个，失败 {failed} 个"
    )


if __name__ == "__main__":
    asyncio.run(migrate())
