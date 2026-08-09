"""load_skill / read_skill_resource 工具：按名加载技能指令与捆绑资源。

渐进式披露（对标 Claude Code skill 规范）：
  - load_skill            加载 SKILL.md 核心指令，并列出可按需读取的资源清单
  - read_skill_resource   按相对路径读取 references/examples/scripts 下的细节文件
"""

from pathlib import Path

from src.llm.tools.skills import get_skill_manager
from src.utils import console

_MAX_SKILL_CHARS = 20000
"""load_skill / read_skill_resource 返回内容最大字符数，超出部分截断（对标 Muika）"""

_RESOURCE_DIRS = ("references", "examples", "scripts")
"""技能捆绑资源目录：这些目录下的文件不随 SKILL.md 自动加载，由 LLM 按需读取"""


def _list_skill_resources(skill_dir: Path) -> list[str]:
    """列出技能目录下可按需读取的捆绑资源（相对技能目录的路径，按名称排序）。"""
    resources: list[str] = []
    for sub in _RESOURCE_DIRS:
        base = skill_dir / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                resources.append(p.relative_to(skill_dir).as_posix())
    return resources


def _format_skill_text(
    skill_name: str,
    skill_dir: Path,
    text: str,
    source: str,
    file_label: str,
) -> str:
    """统一封装技能/资源返回文本（含截断标记与资源清单提示）。"""
    resources = _list_skill_resources(skill_dir)
    truncated = ""
    if len(text) > _MAX_SKILL_CHARS:
        text = text[:_MAX_SKILL_CHARS]
        truncated = f"\n\n[{source}] 内容过长，已截断至 {_MAX_SKILL_CHARS} 字符。"

    resource_hint = ""
    if resources:
        resource_hint = (
            "\n\n该技能捆绑了以下细节资源（用 read_skill_resource 工具按相对路径按需读取）：\n"
            + "\n".join(f"- {r}" for r in resources)
        )

    return (
        f"Skill: {skill_name}\n"
        f"{source} file: {file_label}\n"
        f"该技能引用的其他文件位于：{skill_dir}\n\n"
        f"{text}{truncated}{resource_hint}"
    )


async def _load_skill(skill_name: str) -> str:
    """按名加载技能完整指令（严格参照 Muika _skill.py 的 load_skill 工具）。"""
    manager = get_skill_manager()
    skill = manager.get(skill_name)

    if skill is None:
        available = ", ".join(s.name for s in manager.skills) or "(none)"
        return f"错误：技能 {skill_name!r} 不存在。可用技能：{available}"

    try:
        text = skill.location.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return (
            f"技能文件在扫描后被删除：{skill.location}。下次重扫后会从注册表中移除。"
        )
    except Exception as e:
        console.error(f"[load_skill] 读取失败 {skill.location}: {e}")
        return f"错误：读取技能文件失败：{e}"

    console.dim(f"[load_skill] 已加载技能 '{skill.name}'（{skill.location}）")
    return _format_skill_text(
        skill.name, skill.location.parent, text, "load_skill", str(skill.location)
    )


async def _read_skill_resource(skill_name: str, resource_path: str) -> str:
    """按相对路径读取技能捆绑资源（references/examples/scripts 下的文件）。

    路径必须解析后仍位于技能目录内，防止 ../ 路径穿越。
    """
    manager = get_skill_manager()
    skill = manager.get(skill_name)
    if skill is None:
        return f"错误：技能 {skill_name!r} 不存在。先用 load_skill 确认技能名。"

    skill_dir = skill.location.parent.resolve()
    target = (skill_dir / resource_path).resolve()
    try:
        target.relative_to(skill_dir)
    except ValueError:
        console.warn(f"[read_skill_resource] 越界路径被拒绝: {skill_name}/{resource_path}")
        return f"错误：资源路径 {resource_path!r} 超出技能目录范围。"

    if not target.is_file():
        return f"错误：技能 {skill_name!r} 下不存在资源 {resource_path!r}。"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        console.error(f"[read_skill_resource] 读取失败 {target}: {e}")
        return f"错误：读取资源文件失败：{e}"

    console.dim(f"[read_skill_resource] 已加载 {skill_name}/{resource_path}（{target}）")
    return _format_skill_text(
        skill.name, skill_dir, text, "read_skill_resource", f"{skill_name}/{resource_path}"
    )
