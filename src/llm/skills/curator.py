"""技能 Curator 增强（对标 Hermes agent/curator.py + curator_backup.py 精简落地）。

3.6：在既有技能审阅流程（evolution.skills.maybe_prune 的合并/归档）之上，
补齐「快照可回滚 + 审计日志」两个闭环：

- snapshot_skills：审查变更前把全部技能目录快照到 DATA_ROOT/skills_snapshots/<ts>/；
- rollback_skills：按快照整批恢复（目标技能目录已存在同名时先备份移走，防覆盖丢失）；
- append_curator_ledger：每个归档/合并动作追加审计日志（DATA_ROOT/curator_ledger.jsonl）；
- curator_enabled：由 ENABLE_CURATOR 开关门控——关闭时上述增强全部跳过，
  归档/合并流程与既有行为完全一致（增量层，红线：业务逻辑 100% 不变）。

技能根目录取 SKILLS_DIR 首个段（与 evolution/skills.py 落盘口径一致）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from src.utils import config, console

# 快照根目录：DATA_ROOT/skills_snapshots/<ts>/<技能名>/
_SNAPSHOTS_DIRNAME = "skills_snapshots"

# 审计日志：DATA_ROOT/curator_ledger.jsonl（每行一条 JSON）
_LEDGER_FILENAME = "curator_ledger.jsonl"

# 技能内容 blob 根目录：DATA_ROOT/curator_blobs/<entry_id>/<技能名>/（单条目回滚用）
_BLOBS_DIRNAME = "curator_blobs"

_lock = threading.Lock()
"""保护快照目录枚举/恢复的互斥（后台审查与人工操作可能并发）"""


def _skills_root() -> Optional[Path]:
    """技能根目录（SKILLS_DIR 首个段，相对项目根）；无效返回 None。"""
    root = (config.cfg.SKILLS_DIR or "src/llm/skills").split(",")[0].strip()
    return Path(config.cfg.PROJECT_ROOT) / root if root else None


def _snapshots_root() -> Path:
    return Path(config.cfg.DATA_ROOT) / _SNAPSHOTS_DIRNAME


def curator_enabled() -> bool:
    """Curator 增强开关（ENABLE_CURATOR）：关闭时快照/审计全部跳过。"""
    return bool(config.cfg.ENABLE_CURATOR)


def snapshot_skills(manager=None) -> Optional[str]:
    """把当前全部技能目录快照到快照根，返回快照 id（时间戳）。

    - 无需 manager 参数（按 SKILLS_DIR 直接枚举 <技能根>/*/SKILL.md）；
      传入 manager 仅用于兼容测试注入，不改变行为；
    - 快照目录已存在同名时跳过（防覆盖），失败只告警不抛；
    - 仅复制有 SKILL.md 的技能目录（不含 _archived 等归档区）。
    """
    if not curator_enabled():
        return None
    root = _skills_root()
    if root is None or not root.is_dir():
        return None
    snapshot_id = time.strftime("%Y%m%d-%H%M%S")
    with _lock:
        dest = _snapshots_root() / snapshot_id
        if dest.exists():
            console.warn(f"[Curator] 快照目录已存在，跳过：{dest}")
            return None
        dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        for skill_md in sorted(root.glob("*/SKILL.md")):
            skill_dir = skill_md.parent
            try:
                shutil.copytree(str(skill_dir), str(dest / skill_dir.name))
                copied += 1
            except OSError as e:
                console.warn(f"[Curator] 快照技能 {skill_dir.name!r} 失败：{e}")
    if copied:
        console.dim(f"[Curator] 技能快照：{snapshot_id}（{copied} 个技能）")
    return snapshot_id


def list_snapshots() -> list[str]:
    """当前全部快照 id（按名称排序，新→旧）。"""
    root = _snapshots_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _blobs_root() -> Path:
    """技能内容 blob 根目录（DATA_ROOT/curator_blobs/）。"""
    return Path(config.cfg.DATA_ROOT) / _BLOBS_DIRNAME


def _dir_sha256(skill_dir: Path) -> Optional[str]:
    """对技能目录全部文件内容计算 sha256（按相对路径排序，确定性可复现）。

    归档前/后各算一次，供审计日志核对内容是否被改动（对标 hermes
    skill_ledger 的内容寻址思想：blob 按 sha256 引用，回滚前校验 blob 存在）。
    """
    if not skill_dir.is_dir():
        return None
    digest = hashlib.sha256()
    try:
        files = [p for p in skill_dir.rglob("*") if p.is_file()]
    except OSError:
        return None
    for p in sorted(files):
        digest.update(str(p.relative_to(skill_dir).as_posix()).encode("utf-8"))
        try:
            digest.update(p.read_bytes())
        except OSError:
            return None
    return digest.hexdigest()


def capture_before(skill_name: str) -> Optional[str]:
    """变更前捕获技能目录完整内容到 blob 区，返回 entry_id（供回滚定位）。

    - 仅当 Curator 增强开启时生效；技能不存在/复制失败返回 None（调用方照常执行）；
    - blob 目录 <entry_id>/<技能名>/ 内为变更前原始内容，rollback_entry 按它恢复；
    - 追加一条 action=capture 的审计日志关联该 entry_id。
    """
    if not curator_enabled():
        return None
    root = _skills_root()
    if root is None:
        return None
    skill_dir = root / skill_name
    if not skill_dir.is_dir():
        return None
    entry_id = uuid.uuid4().hex[:12]
    dest = _blobs_root() / entry_id / skill_name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(skill_dir), str(dest))
    except OSError as e:
        console.warn(f"[Curator] 捕获技能 {skill_name!r} 失败：{e}")
        return None
    append_curator_ledger("capture", skill_name,
                          detail=f"blob={entry_id}", entry_id=entry_id)
    return entry_id


def append_curator_ledger(action: str, skill_name: str,
                          reason: str = "", detail: str = "", *,
                          entry_id: str = "", before: str = "",
                          after: str = "", actor: str = "curator",
                          absorbed_into: str = "") -> None:
    """追加一条 Curator 审计日志（动作 / 技能 / 时间 / 原因 / 内容指纹）。

    - 文件为 JSON Lines（DATA_ROOT/curator_ledger.jsonl），追加写、不覆写；
    - before/after 为变更前后技能目录的 sha256（before_sha256/after_sha256），
      与 entry_id 指向的 blob 共同支撑单条目回滚（rollback_entry）；
    - absorbed_into：合并场景下被吸收技能的前进目标技能名（5.10 引用迁移
      依赖它做 load_skill 旧名兼容 / scheduler 任务改写），无则留空；
    - actor 标记变更来源（curator/agent/user，默认 curator）；
    - 开关关闭或写失败仅告警，不影响主流程。
    """
    if not curator_enabled():
        return
    entry = {
        "entry_id": entry_id or uuid.uuid4().hex[:12],
        "actor": actor,                # curator / agent / user（溯源用）
        "action": action,              # capture / archive / merge / merge_patch / rollback
        "skill": skill_name,
        "absorbed_into": absorbed_into,  # 5.10：合并目标技能名（引用迁移依据）
        "before_sha256": before,
        "after_sha256": after,
        "reason": reason,
        "detail": detail,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = Path(config.cfg.DATA_ROOT) / _LEDGER_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        console.warn(f"[Curator] 审计日志写入失败：{e}")


def list_entries() -> list[dict]:
    """读取全部审计日志条目（新→旧）；文件缺失/损坏返回空列表。"""
    path = Path(config.cfg.DATA_ROOT) / _LEDGER_FILENAME
    entries = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(reversed(entries))


def get_entry(entry_id: str) -> Optional[dict]:
    """按 entry_id 查找审计条目；不存在返回 None。"""
    for entry in list_entries():
        if entry.get("entry_id") == entry_id:
            return entry
    return None


def rollback_entry(entry_id: str) -> bool:
    """按单条目恢复技能（从 blob 区复制回技能根），返回是否成功。

    - 回滚前先捕获当前状态为 safety blob（回滚本身可再回滚）；
    - blob 缺失/审计条目不存在时不执行（fail-closed）；
    - 目标已存在同名目录时先备份移入 _archived/_rollback_backup_<ts>（不覆盖）。
    """
    if not curator_enabled():
        return False
    entry = get_entry(entry_id)
    if entry is None:
        console.warn(f"[Curator] 回滚失败：审计条目不存在 {entry_id!r}")
        return False
    skill_name = entry.get("skill") or ""
    blob_dir = _blobs_root() / entry_id / skill_name
    if not blob_dir.is_dir():
        console.warn(f"[Curator] 回滚失败：blob 不存在 {entry_id!r}/{skill_name}")
        return False
    root = _skills_root()
    if root is None:
        return False
    # 回滚前先保护当前状态（safety 快照），使回滚操作本身可撤销
    safety_id = capture_before(skill_name)
    backup_dir = root / "_archived" / f"_rollback_backup_{int(time.time())}"
    target = root / skill_name
    with _lock:
        try:
            if target.exists():
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backup_dir / skill_name))
            shutil.copytree(str(blob_dir), str(target))
        except OSError as e:
            console.warn(f"[Curator] 回滚 {skill_name!r} 失败：{e}")
            return False
    append_curator_ledger(
        "rollback", skill_name,
        reason=f"rollback entry {entry_id}",
        detail=f"backup={backup_dir.name}, safety={safety_id or '-'}",
        entry_id=entry_id, after=_dir_sha256(target) or "")
    console.ok(f"[Curator] 已回滚技能 {skill_name!r}（entry {entry_id}）")
    return True


def rollback_skills(snapshot_id: str) -> int:
    """按快照整批恢复技能目录，返回恢复的技能数。

    - 快照内每个技能目录复制回技能根；目标已存在同名目录时先备份移入
      <技能根>/_archived/_rollback_backup_<ts>（不覆盖不丢失）；
    - 快照不存在/为空返回 0；失败只告警不抛。
    """
    if not curator_enabled():
        return 0
    root = _skills_root()
    snap = _snapshots_root() / snapshot_id
    if root is None or not snap.is_dir():
        console.warn(f"[Curator] 回滚失败：快照不存在 {snapshot_id!r}")
        return 0
    backup_dir = root / "_archived" / f"_rollback_backup_{int(time.time())}"
    restored = 0
    with _lock:
        for skill_dir in sorted(snap.iterdir()):
            if not skill_dir.is_dir():
                continue
            target = root / skill_dir.name
            try:
                if target.exists():
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(backup_dir / skill_dir.name))
                shutil.copytree(str(skill_dir), str(target))
                restored += 1
            except OSError as e:
                console.warn(f"[Curator] 恢复技能 {skill_dir.name!r} 失败：{e}")
    if restored:
        console.ok(f"[Curator] 回滚快照 {snapshot_id}：恢复 {restored} 个技能")
    return restored
