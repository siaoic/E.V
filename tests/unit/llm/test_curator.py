"""3.6 Curator（快照 / 回滚 / 审计）单元测试。

用临时目录搭技能根，验证：快照 → 归档技能 → 按快照整批回滚；
审计日志 JSONL 追加；ENABLE_CURATOR=0 时快照/审计跳过（零副作用）。
"""
import json
import pytest
from pathlib import Path
from types import SimpleNamespace

import ev.utils.config as config
from ev.llm.evolution._utils import archive_skill
from ev.llm.skills import curator


@pytest.fixture
def env(tmp_path, monkeypatch):
    """替换 config.cfg 指向临时目录：技能根 / 数据根（快照与审计落此）。"""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "skill_a").mkdir()
    (skills_root / "skill_a" / "SKILL.md").write_text(
        "---\nname: skill_a\ndescription: A 技能\n---\n正文 A\n", encoding="utf-8")
    (skills_root / "skill_b").mkdir()
    (skills_root / "skill_b" / "SKILL.md").write_text(
        "---\nname: skill_b\ndescription: B 技能\n---\n正文 B\n", encoding="utf-8")
    cfg = SimpleNamespace(
        SKILLS_DIR="skills",
        PROJECT_ROOT=tmp_path,
        DATA_ROOT=str(tmp_path / "data"),
        ENABLE_CURATOR=True,
    )
    monkeypatch.setattr(config, "cfg", cfg)
    return SimpleNamespace(root=tmp_path, skills_root=skills_root,
                           data_root=cfg.DATA_ROOT)


class _Skill:
    """archive_skill 需要的轻量技能对象（location 指向 SKILL.md 文件）。"""

    def __init__(self, name, location):
        self.name = name
        self.location = Path(location)


class TestSnapshotAndRollback:
    def test_snapshot_then_archive_then_rollback(self, env):
        root = env.skills_root
        snapshot_id = curator.snapshot_skills()
        assert snapshot_id is not None
        snap_dir = Path(env.data_root) / "skills_snapshots" / snapshot_id
        assert (snap_dir / "skill_a" / "SKILL.md").is_file()
        assert (snap_dir / "skill_b" / "SKILL.md").is_file()

        # 归档 skill_a 后：技能根消失，归档区出现
        skill_a = _Skill("skill_a", root / "skill_a" / "SKILL.md")
        assert archive_skill(skill_a) is True
        assert not (root / "skill_a").exists()
        assert (root / "_archived" / "skill_a").is_dir()

        # 按快照整批回滚：skill_a 恢复，skill_b 原样
        restored = curator.rollback_skills(snapshot_id)
        assert restored == 2
        assert (root / "skill_a" / "SKILL.md").is_file()
        assert "正文 A" in (root / "skill_a" / "SKILL.md").read_text(encoding="utf-8")
        assert (root / "skill_b" / "SKILL.md").is_file()

    def test_rollback_missing_snapshot_returns_zero(self, env):
        assert curator.rollback_skills("no-such-snapshot") == 0

    def test_rollback_backs_up_existing_target(self, env):
        root = env.skills_root
        snapshot_id = curator.snapshot_skills()
        # 归档并人为改写技能根的同名目录（模拟被改坏），回滚不应覆盖丢失
        skill_a = _Skill("skill_a", root / "skill_a" / "SKILL.md")
        archive_skill(skill_a)
        (root / "skill_a").mkdir()
        (root / "skill_a" / "SKILL.md").write_text("污染内容", encoding="utf-8")
        restored = curator.rollback_skills(snapshot_id)
        assert restored == 2
        assert "正文 A" in (root / "skill_a" / "SKILL.md").read_text(encoding="utf-8")
        backups = list((root / "_archived").glob("_rollback_backup_*"))
        assert backups  # 被覆盖前已备份
        assert (backups[0] / "skill_a" / "SKILL.md").is_file()

    def test_list_snapshots_sorted(self, env):
        curator.snapshot_skills()
        snapshots = curator.list_snapshots()
        assert len(snapshots) == 1
        assert snapshots == sorted(snapshots, reverse=True)


class TestAuditLedger:
    def test_append_ledger_writes_jsonl(self, env):
        curator.append_curator_ledger("archive", "skill_a", reason="低使用率",
                                      detail="snapshot=x")
        ledger = Path(env.data_root) / "curator_ledger.jsonl"
        assert ledger.is_file()
        line = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[0])
        assert line["action"] == "archive"
        assert line["skill"] == "skill_a"
        assert line["reason"] == "低使用率"

    def test_append_appends_not_overwrites(self, env):
        curator.append_curator_ledger("archive", "a")
        curator.append_curator_ledger("merge", "b")
        ledger = Path(env.data_root) / "curator_ledger.jsonl"
        lines = ledger.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["action"] == "merge"


class TestDisabled:
    def test_snapshot_skipped_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "cfg", SimpleNamespace(
            SKILLS_DIR="skills", PROJECT_ROOT=tmp_path,
            DATA_ROOT=str(tmp_path / "data"), ENABLE_CURATOR=False))
        assert curator.snapshot_skills() is None
        assert not (Path(tmp_path / "data" / "skills_snapshots")).exists()

    def test_ledger_skipped_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "cfg", SimpleNamespace(
            SKILLS_DIR="skills", PROJECT_ROOT=tmp_path,
            DATA_ROOT=str(tmp_path / "data"), ENABLE_CURATOR=False))
        curator.append_curator_ledger("archive", "a")
        assert not (Path(tmp_path / "data" / "curator_ledger.jsonl")).exists()
