"""数据根路径管理：可写数据根（DATA_ROOT）与内置资源首次同步。

可写/只读数据根分离（对标 Firefly paths.py）：
- 只读资源根 = PROJECT_ROOT：源码、内置技能、话题种子等，随代码包发布
- 可写数据根 = DATA_ROOT：记忆库、音频缓存、知识库等运行时数据，默认
  <PROJECT_ROOT>/data，可用环境变量 E_V_DATA_DIR 重定向到独立目录

内置资源首次同步：随仓库发布的知识库种子（facts.yaml / persona_lore.md /
curated_cards / world_lore）在数据根缺失时自动复制——用户改过的文件
不会被覆盖（目标已存在即跳过），保证全新数据根开箱即有知识库。
"""

from __future__ import annotations

import os
import shutil

from ev.utils import config, console

# 数据文件归类布局：子目录 → 该目录下的文件名清单。运行时数据一律
# 落子目录（数据根只留 knowledge/ / tts_cache/ 等资源目录与静态词库）。
# 新增数据文件时在此登记，ensure_data_dirs 会自动建目录并迁移旧版平铺文件。
_DATA_LAYOUT: dict[str, tuple[str, ...]] = {
    "agent": (  # 委派队列 + 建议存储
        "delegation.db",
        "agent_suggestions.json",
        "agent_suggestions_dismissed.json",
    ),
    "memory": (  # 记忆库（会话历史 / memU 向量库 / 图谱快照）
        "history.db",
        "memu.sqlite3",
        "memory_graph.json",
    ),
    "evolution": (  # 自我进化引擎产物 + LLM 调用记账
        "evolution_advice.md",
        "evolution_advice_active.json",
        "evolution_evals.jsonl",
        "evolution_feedback.jsonl",
        "evolution_policy.json",
        "evolution_policy_history.md",
        "evolution_profile.json",
        "evolution_profile_history.jsonl",
        "evolution_usage.jsonl",
        "aux_usage.jsonl",
        "skill_usage.json",
    ),
    "vts": (  # VTS 令牌 / 表情库 / 情绪映射（pet 与 vtuber 各一）
        "vts_token.json",
        "vts_face_lib.json",
        "emotion_map.json",
        "emotion_map_vts.json",
    ),
}

# SQLite WAL 侧车后缀：迁移库文件时一并搬运
_DB_SIDECARS = ("-wal", "-shm")


def ensure_data_dirs() -> None:
    """集中创建可写数据根及标准子目录（幂等，失败静默不阻塞启动）。"""
    data_root = config.cfg.DATA_ROOT
    for sub in (
        "",  # data/
        *_DATA_LAYOUT,  # agent/ memory/ evolution/ vts/ 归类子目录
        os.path.join("knowledge", "curated_cards"),
        os.path.join("knowledge", "world_lore"),
        "tts_cache",
    ):
        try:
            os.makedirs(os.path.join(data_root, sub), exist_ok=True)
        except OSError:
            continue
    _migrate_flat_files()


def _migrate_flat_files() -> None:
    """一次性迁移：旧版平铺在数据根的文件搬入归类子目录（幂等）。

    仅当旧路径存在且新路径不存在时移动（绝不覆盖新位置已有文件）；
    SQLite 库文件连带 -wal / -shm 侧车一并处理。文件被占用（另一进程
    正打开）时移动失败静默跳过，下次启动重试。
    """
    root = config.cfg.DATA_ROOT
    moved = 0
    for sub, names in _DATA_LAYOUT.items():
        for name in names:
            for suffix in ("",) + _DB_SIDECARS:
                old = os.path.join(root, name + suffix)
                if not os.path.isfile(old):
                    continue
                new = os.path.join(root, sub, name + suffix)
                try:
                    if os.path.exists(new):
                        continue
                    shutil.move(old, new)
                    moved += 1
                except OSError:
                    continue
    if moved:
        console.dim(f"[数据根] 已把 {moved} 个历史数据文件归入子目录（{root}）")


def sync_builtin_resources() -> None:
    """首次运行把内置知识库种子同步到可写数据根（幂等）。

    种子源为仓库自带的 data/knowledge/；仅当目标缺失对应文件时才复制，
    用户编辑过的文件不会被覆盖。数据根未重定向（默认场景）时源与目标
    相同，直接跳过。
    """
    src_root = os.path.join(config.cfg.PROJECT_ROOT, "data", "knowledge")
    dst_root = os.path.join(config.cfg.DATA_ROOT, "knowledge")
    if not os.path.isdir(src_root) or os.path.normpath(src_root) == os.path.normpath(dst_root):
        return
    copied = 0
    try:
        os.makedirs(dst_root, exist_ok=True)
        for dirpath, _dirs, files in os.walk(src_root):
            rel = os.path.relpath(dirpath, src_root)
            target_dir = dst_root if rel == "." else os.path.join(dst_root, rel)
            os.makedirs(target_dir, exist_ok=True)
            for name in files:
                src = os.path.join(dirpath, name)
                dst = os.path.join(target_dir, name)
                if not os.path.isfile(dst):
                    shutil.copy2(src, dst)
                    copied += 1
    except OSError:
        return
    if copied:
        console.dim(f"[数据根] 已补齐内置知识库 {copied} 个文件（{dst_root}）")
