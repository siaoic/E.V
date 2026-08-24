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


def ensure_data_dirs() -> None:
    """集中创建可写数据根及标准子目录（幂等，失败静默不阻塞启动）。"""
    data_root = config.cfg.DATA_ROOT
    for sub in (
        "",  # data/
        os.path.join("knowledge", "curated_cards"),
        os.path.join("knowledge", "world_lore"),
        "tts_cache",
    ):
        try:
            os.makedirs(os.path.join(data_root, sub), exist_ok=True)
        except OSError:
            continue


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
