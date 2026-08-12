"""技能管理器 —— 严格参照 Muika-After-Story 1.4.1 的 muika/plugin/skills.py。

技能 = 一组打包的指令集（SKILL.md），按需加载：
  - 系统提示只注入技能「名 + 描述」列表（轻量，不占上下文）
  - LLM 通过 load_skill 工具按名加载完整 SKILL.md 全文
  - watchdog 监听技能目录，热重载（改技能文件无需重启）
"""

from __future__ import annotations

import atexit
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from src.utils import config, console

# 技能使用统计持久化文件：record_usage 记录 load 次数与最近使用时间，
# 复盘时注入进化引擎，让技能修补/清理有真实使用数据支撑（对标 hermes 的
# skill usage counters——但 hermes 明确 use=0 不作为归档依据，这里同样只供参考）
_USAGE_PATH = os.path.join(config.cfg.PROJECT_ROOT, "data", "skill_usage.json")

_SKILL_FILENAME = "SKILL.md"
"""技能定义文件名"""

_DESCRIPTION_MAX_CHARS = 200
"""frontmatter 缺失时，回退描述的最大长度"""

_FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
"""匹配文件开头的 YAML frontmatter 块"""


@dataclass(frozen=True)
class AgentSkill:
    """一个可用技能的元数据"""

    name: str
    description: str
    location: Path  # 指向 SKILL.md 的绝对路径
    pinned: bool = False  # frontmatter pinned: true → 禁止自动修补/合并/归档


def _parse_skill_md(path: Path) -> tuple[str, str, bool]:
    """
    解析 SKILL.md，提取技能名、描述与 pin 状态。

    优先读取 YAML frontmatter 中的 name / description / pinned；
    frontmatter 缺失或损坏时回退：name 取目录名，description 取首个 Markdown 标题，
    再退化为首个非空行（截断至 _DESCRIPTION_MAX_CHARS）；pinned 回退 False。

    :param path: SKILL.md 文件路径
    :return: (name, description, pinned)
    """
    fallback_name = path.parent.name
    pinned = False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        console.warn(f"读取技能文件失败 {path}: {e}")
        return fallback_name, "", pinned

    name: Optional[str] = None
    description: Optional[str] = None

    match = _FRONTMATTER_PATTERN.match(text)
    if match:
        try:
            meta = yaml.safe_load(match.group(1))
            if isinstance(meta, dict):
                if isinstance(meta.get("name"), str) and meta["name"].strip():
                    name = meta["name"].strip()
                if isinstance(meta.get("description"), str) and meta["description"].strip():
                    description = meta["description"].strip()
                # frontmatter pinned: true → 该技能受保护（对标 hermes curator pin）
                pinned = bool(meta.get("pinned"))
        except yaml.YAMLError:
            # frontmatter 损坏时静默回退
            pass

    if name is None:
        name = fallback_name

    if description is None:
        body = text[match.end():] if match else text
        description = ""
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # 首个 Markdown 标题优先，否则取首个非空行
            description = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
            break
        description = description[:_DESCRIPTION_MAX_CHARS]

    return name, description, pinned


def _scan_roots(roots: Iterable[Path]) -> dict[str, AgentSkill]:
    """
    扫描各根目录下的 <skill-dir>/SKILL.md，构建技能注册表。

    后出现的根目录覆盖先前的（调用方应按 优先级低 -> 高 的顺序传入，
    使靠后的技能根目录优先于靠前的）。

    :param roots: 技能根目录列表
    :return: name -> AgentSkill 的映射
    """
    skills: dict[str, AgentSkill] = {}
    for root in roots:
        if not root.is_dir():
            console.dim(f"[Skills] 技能目录不存在，跳过: {root}")
            continue
        for skill_md in sorted(root.glob(f"*/{_SKILL_FILENAME}")):
            name, description, pinned = _parse_skill_md(skill_md)
            if name in skills:
                console.warn(f"[Skills] 技能名冲突: '{name}' ({skill_md}) 将覆盖 ({skills[name].location})")
            skills[name] = AgentSkill(name=name, description=description,
                                      location=skill_md.resolve(), pinned=pinned)
    return skills


class SkillFileHandler(FileSystemEventHandler):
    """技能目录变化处理器：任何文件变动都触发一次全量重扫。

    采用 trailing 防抖（事件后冷却期内无新事件才执行重扫）：
    编辑器保存 / 新建技能目录往往产生一连串事件（create→modify→write），
    防抖合并后保证重扫时文件已写入完成。
    """

    def __init__(self, callback):
        self.callback = callback
        self.cooldown = 1  # 冷却时间（秒）
        self._timer: Optional[threading.Timer] = None

    def _on_any_event(self, event):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.cooldown, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self._timer = None
        try:
            self.callback()
        except Exception:
            pass

    on_created = _on_any_event
    on_modified = _on_any_event
    on_deleted = _on_any_event
    on_moved = _on_any_event


class SkillManager:
    """技能管理器：启动时扫描技能目录，并通过文件监听实现热重载"""

    _instance: Optional["SkillManager"] = None
    _lock = threading.Lock()
    _initialized: bool

    def __new__(cls):
        """确保实例在单例模式下运行"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SkillManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self._skills: dict[str, AgentSkill] = {}
        """当前技能注册表（name -> AgentSkill），替换时整体原子交换"""
        self._skills_lock = threading.Lock()
        """保护 _skills 替换（观察者线程写入，asyncio 线程读取）"""
        self._usage: dict[str, dict] = self._load_usage()
        """技能使用统计（name -> {loads, last_used}），持久化在 data/skill_usage.json"""
        self.observer: Optional[BaseObserver] = None
        """文件监视器"""
        self._watched_roots: set[Path] = set()
        """已调度的监听根目录，避免重复 schedule"""

        self.reload()
        self._start_watcher()

        self._initialized = True

    def _skill_roots(self) -> list[Path]:
        """按优先级从低到高返回技能根目录（.env SKILLS_DIR 逗号分隔，相对项目根）"""
        roots: list[Path] = []
        for seg in (config.cfg.SKILLS_DIR or "skills").split(","):
            seg = seg.strip()
            if seg:
                roots.append(Path(config.cfg.PROJECT_ROOT) / seg)
        return roots

    def reload(self) -> None:
        """全量重扫技能目录，原子替换注册表；同时为新出现的根目录补充监听"""
        skills = _scan_roots(self._skill_roots())
        with self._skills_lock:
            self._skills = skills
        console.dim(f"[Skills] 技能扫描完成，共 {len(skills)} 个: {', '.join(skills) or '(无)'}")
        self._watch_new_roots()

    def _start_watcher(self) -> None:
        """启动文件监视器"""
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()

        self.observer = Observer()
        self._event_handler = SkillFileHandler(self._on_skills_changed)
        self._watched_roots = set()
        self._watch_new_roots()
        self.observer.start()

    def _watch_new_roots(self) -> None:
        """为尚未监听的已存在根目录调度监听（处理运行期间才创建的目录）"""
        if self.observer is None:
            return
        for root in self._skill_roots():
            if root.is_dir() and root not in self._watched_roots:
                self.observer.schedule(self._event_handler, str(root), recursive=True)
                self._watched_roots.add(root)

    def _on_skills_changed(self) -> None:
        """技能目录变化回调"""
        try:
            self.reload()
        except Exception as e:
            console.error(f"[Skills] 重新扫描技能目录失败: {e}")

    def get(self, name: str) -> Optional[AgentSkill]:
        """按名称获取技能元数据，不存在时返回 None"""
        with self._skills_lock:
            return self._skills.get(name)

    @property
    def skills(self) -> list[AgentSkill]:
        """当前全部技能的快照副本"""
        with self._skills_lock:
            return list(self._skills.values())

    def render_prompt_section(self) -> str:
        """
        生成注入系统提示的技能元数据段落（仅 name + description）。
        无可用技能时返回空字符串。
        """
        skills = self.skills
        if not skills:
            return ""
        lines = [
            "Available skills (use the load_skill tool with the exact skill name "
            "to load full instructions before attempting the task):"
        ]
        lines.extend(f"- {s.name}: {s.description}" for s in skills)
        return "\n".join(lines)

    def stop_watcher(self) -> None:
        """停止文件监视器"""
        if self.observer is None:
            return
        self.observer.stop()
        self.observer.join()

    # ---------- 技能使用统计（进化引擎数据源） ----------

    def _load_usage(self) -> dict[str, dict]:
        """读取技能使用统计（文件缺失/损坏时返回空字典）。"""
        try:
            with open(_USAGE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_usage(self) -> None:
        """覆写技能使用统计文件（失败静默，统计丢失不影响功能）。"""
        try:
            os.makedirs(os.path.dirname(_USAGE_PATH), exist_ok=True)
            with open(_USAGE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._usage, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def record_usage(self, name: str) -> None:
        """记录一次技能使用：load 次数 +1 并更新最近使用时间。"""
        with self._skills_lock:
            entry = self._usage.setdefault(name, {"loads": 0, "last_used": 0.0})
            entry["loads"] = int(entry.get("loads") or 0) + 1
            entry["last_used"] = time.time()
            self._save_usage()

    def usage_of(self, name: str) -> Optional[dict]:
        """返回单个技能的使用统计（{loads, last_used}），未使用过返回 None。"""
        with self._skills_lock:
            entry = self._usage.get(name)
            return dict(entry) if entry else None

    def usage_section(self) -> str:
        """当前全部技能的使用统计文本段（复盘注入用），无数据返回空串。"""
        with self._skills_lock:
            usage = dict(self._usage)
        if not usage:
            return ""
        lines = []
        for name, entry in sorted(usage.items()):
            loads = int(entry.get("loads") or 0)
            last_used = entry.get("last_used") or 0.0
            stamp = (time.strftime("%m-%d %H:%M", time.localtime(last_used))
                     if last_used else "从未")
            lines.append(f"- {name}: loads={loads}, last_used={stamp}")
        return "\n".join(lines)


_skill_manager: Optional[SkillManager] = None


def get_skill_manager() -> SkillManager:
    """获取技能管理器单例（首次调用时执行启动扫描并启动监听）"""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
        atexit.register(_skill_manager.stop_watcher)
    return _skill_manager
