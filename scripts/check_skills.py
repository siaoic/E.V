"""技能文件校验脚本：检查每个 SKILL.md 的 frontmatter 是否符合规范。

规范（对齐 Hermes Agent SKILL.md 标准）：
  - 必填：name（非空）、description（≤60 字符）、version（语义化版本）
  - 可选：author / license / platforms / metadata.hermes.tags

用法：
  python scripts/check_skills.py [技能根目录 ...]
不传目录时默认扫描 src/llm/skills（及 .env SKILLS_DIR 配置的其余目录）。
只读校验，不修改任何文件；任一技能不通过则退出码非 0。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 允许脚本在仓库任意目录下运行：把项目根加入 sys.path 以复用 src.utils.config
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_ROOT = _PROJECT_ROOT / "src" / "llm" / "skills"
_SKILL_FILENAME = "SKILL.md"
_DESCRIPTION_MAX = 60  # Hermes 标准：description 最多 60 字符
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)

_FIELD_CHECKS = (
    ("name", "必填字段，非空"),
    ("description", f"必填字段，≤{_DESCRIPTION_MAX} 字符"),
    ("version", "必填字段，形如 1.0.0"),
)


def _load_frontmatter(path: Path) -> tuple[dict | None, str]:
    """解析 SKILL.md 的 YAML frontmatter。

    返回 (meta, error)；meta 为 None 表示解析失败，error 说明原因。
    用现有依赖 pyyaml 解析（技能管理器同样依赖它），只读不写。
    """
    import yaml

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"读取失败：{e}"
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, "缺少 frontmatter（文件须以 --- 开头）"
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        return None, f"YAML 解析失败：{e}"
    if not isinstance(meta, dict):
        return None, "frontmatter 顶层必须是键值映射"
    return meta, ""


def check_skill(path: Path) -> list[str]:
    """校验单个 SKILL.md，返回违反规则的说明列表（空 = 通过）。"""
    meta, error = _load_frontmatter(path)
    if error:
        return [error]

    issues: list[str] = []
    for field, rule in _FIELD_CHECKS:
        value = meta.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{field}: {rule}")
            continue
        if field == "description" and len(value.strip()) > _DESCRIPTION_MAX:
            issues.append(f"description: 长度 {len(value.strip())} 超过 {_DESCRIPTION_MAX} 字符")
        if field == "version" and not _VERSION_RE.match(value.strip()):
            issues.append(f"version: 不是语义化版本号：{value.strip()!r}")

    # name 应与目录名一致（技能管理器以 name 注册，避免同名冲突）
    name = meta.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != path.parent.name:
        issues.append(f"name: {name.strip()!r} 与目录名 {path.parent.name!r} 不一致")
    return issues


def main(argv: list[str]) -> int:
    """主入口：扫描根目录并输出校验结果。"""
    roots = [Path(p) for p in argv] if argv else [_DEFAULT_ROOT]

    # 技能根目录也可由 .env SKILLS_DIR 配置（逗号分隔，相对项目根）
    if not argv:
        try:
            from ev.utils import config
            for seg in (config.cfg.SKILLS_DIR or "").split(","):
                seg = seg.strip()
                if seg:
                    roots.append(Path(config.cfg.PROJECT_ROOT) / seg)
        except Exception:
            pass  # config 不可用（如未装依赖）时仅用默认目录

    all_issues: list[tuple[Path, list[str]]] = []
    total = 0
    for root in roots:
        if not root.is_dir():
            print(f"SKIP  目录不存在：{root}")
            continue
        for skill_md in sorted(root.glob(f"*/{_SKILL_FILENAME}")):
            total += 1
            issues = check_skill(skill_md)
            if issues:
                all_issues.append((skill_md, issues))
                print(f"FAIL  {skill_md}")
                for issue in issues:
                    print(f"      - {issue}")
            else:
                print(f"PASS  {skill_md}")

    print(f"\n共校验 {total} 个技能，{len(all_issues)} 个不通过。")
    return 1 if all_issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
