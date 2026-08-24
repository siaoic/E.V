"""知识库加载器：启动时一次性加载到内存。

对标 Firefly 的知识金字塔（L0a 精选卡片 → L0b 确定性事实 → L0c 角色亲历
→ L1 世界观），数据存放在 data/knowledge/：
- curated_cards/*.md   L0a：YAML frontmatter（pattern / priority）+ 正文
- facts.yaml           L0b：确定性事实（id / keywords / answer / confidence）
- persona_lore.md      L0c：角色亲历（## 分段，第一人称）
- world_lore/*.md      L1：世界观（## 分段，第三人称）
- *_lore.md            L1：根目录世界观（兼容 Firefly 布局，persona_lore.md 除外）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from ev.utils import config as _config


@dataclass
class CuratedCard:
    id: str
    pattern: re.Pattern  # 编译好的触发正则
    content: str
    priority: int = 0


@dataclass
class Fact:
    id: str
    keywords: list
    answer: str
    confidence: float = 1.0


@dataclass
class LoreBlock:
    id: str
    content: str
    perspective: str  # "first_person" | "third_person"
    topic: str


@dataclass
class KnowledgeBase:
    curated: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    lore: list = field(default_factory=list)


def load_knowledge(root: Optional[str] = None) -> KnowledgeBase:
    """加载 data/knowledge 全量内容（启动时调用一次，进程内缓存复用）。

    root 留空时使用可写数据根下的 knowledge（config.cfg.DATA_ROOT，默认
    <PROJECT_ROOT>/data，可用 E_V_DATA_DIR 重定向；兼容 PyInstaller 打包）。
    """
    base = Path(root) if root else Path(_config.cfg.DATA_ROOT) / "knowledge"
    kb = KnowledgeBase()

    # L0a：curated_cards/*.md（frontmatter 提供 pattern / priority）
    cards_dir = base / "curated_cards"
    if cards_dir.is_dir():
        for f in sorted(cards_dir.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
            if m:
                meta = yaml.safe_load(m.group(1)) or {}
                content = m.group(2).strip()
                try:
                    pattern = re.compile(meta.get("pattern", ""), re.IGNORECASE)
                except re.error:
                    pattern = re.compile("", re.IGNORECASE)  # 非法正则不触发
                priority = int(meta.get("priority", 0) or 0)
            else:
                # 无 frontmatter：用文件名关键字作为提示词
                hint = f.stem.split("-", 1)[-1] if "-" in f.stem else f.stem
                pattern = re.compile(hint, re.IGNORECASE)
                content = text.strip()
                priority = 0
            if content:
                kb.curated.append(CuratedCard(
                    id=f.stem, pattern=pattern, content=content, priority=priority))

    # L0b：facts.yaml
    facts_file = base / "facts.yaml"
    if facts_file.is_file():
        data = yaml.safe_load(facts_file.read_text(encoding="utf-8")) or []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                kb.facts.append(Fact(
                    id=item["id"],
                    keywords=item.get("keywords") or [],
                    answer=item.get("answer") or "",
                    confidence=float(item.get("confidence", 1.0))))

    # L0c：persona_lore.md（## 二级标题分段，第一人称）
    lore_file = base / "persona_lore.md"
    if lore_file.is_file():
        for i, para in enumerate(_split_lore_paragraphs(lore_file.read_text(encoding="utf-8"))):
            if not para["content"].strip():
                continue
            kb.lore.append(LoreBlock(
                id=f"persona-{i}",
                content=para["content"],
                perspective="first_person",
                topic=para.get("topic", "general")))

    # L1：world_lore/*.md（## 二级标题分段，第三人称，topic = 文件名）
    # 兼容 Firefly 数据布局：data/knowledge/ 根目录下的 *_lore.md 同样按
    # 世界观层加载（persona_lore.md 已作为 L0c 角色亲历，跳过）
    world_dir = base / "world_lore"
    lore_files = sorted(world_dir.glob("*.md")) if world_dir.is_dir() else []
    lore_files += [f for f in sorted(base.glob("*_lore.md"))
                   if f.name != "persona_lore.md"]
    for f in lore_files:
        for i, para in enumerate(_split_lore_paragraphs(f.read_text(encoding="utf-8"))):
            if not para["content"].strip():
                continue
            kb.lore.append(LoreBlock(
                id=f"{f.stem}-{i}",
                content=para["content"],
                perspective="third_person",
                topic=para.get("topic", f.stem)))

    return kb


def _split_lore_paragraphs(text: str) -> list:
    """按 ## 二级标题分段，frontmatter 中 title 作 topic。"""
    blocks = []
    current = {"content": "", "topic": "general"}
    for line in text.split("\n"):
        if line.startswith("## "):
            if current["content"].strip():
                blocks.append(current)
            current = {"content": "", "topic": line[3:].strip()}
        else:
            current["content"] += line + "\n"
    if current["content"].strip():
        blocks.append(current)
    return blocks
