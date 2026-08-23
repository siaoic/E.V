"""后台委派队列（3.8）单测：入队 / 重试 / worker 执行 / 开关关闭回退。"""
import time

import pytest

from src.agent.async_delegation import (
    DelegationQueue, DelegationWorker, delegate_backend_enabled,
)


class TestDelegationQueue:
    def test_enqueue_disabled_by_default(self, tmp_path):
        """开关默认关闭：enqueue 返回 None（调用方回退同步路径）。"""
        q = DelegationQueue(tmp_path / "delegation.db")
        assert not delegate_backend_enabled()
        assert q.enqueue("任务") is None

    def test_enqueue_and_claim(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.async_delegation.delegate_backend_enabled",
                            lambda: True)
        q = DelegationQueue(tmp_path / "delegation.db")
        job_id = q.enqueue("后台查 10 个直播间热度")
        assert job_id is not None
        job = q._claim_next()
        assert job is not None and job["id"] == job_id
        assert job["task"] == "后台查 10 个直播间热度"
        # 已领取：再取为空
        assert q._claim_next() is None

    def test_empty_claim(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.async_delegation.delegate_backend_enabled",
                            lambda: True)
        q = DelegationQueue(tmp_path / "delegation.db")
        assert q._claim_next() is None

    def test_retry_schedule_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.async_delegation.delegate_backend_enabled",
                            lambda: True)
        q = DelegationQueue(tmp_path / "delegation.db")
        job_id = q.enqueue("会失败的任务")
        job = q._claim_next()
        q._finish(job_id, False, error="boom")
        # 失败未达上限：状态回到 pending 且排期重试（next_retry 未来）
        row = q._conn.execute(
            "SELECT status, attempts, next_retry FROM delegate_queue WHERE id=?",
            (job_id,)).fetchone()
        assert row[0] == "pending"
        assert row[1] == 1
        assert row[2] > time.time()

    def test_fail_permanent_after_max(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.async_delegation.delegate_backend_enabled",
                            lambda: True)
        q = DelegationQueue(tmp_path / "delegation.db")
        job_id = q.enqueue("任务")
        for _ in range(8):
            job = q._claim_next()
            assert job is not None
            q._finish(job_id, False, error="x")
            # 模拟退避到期：失败后 next_retry 在将来，重置后才能立即再领
            q._conn.execute(
                "UPDATE delegate_queue SET next_retry=0 WHERE id=?", (job_id,))
            q._conn.commit()
        row = q._conn.execute(
            "SELECT status, attempts FROM delegate_queue WHERE id=?",
            (job_id,)).fetchone()
        assert row[0] == "failed"
        assert row[1] == 8

    def test_success_marks_done(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.async_delegation.delegate_backend_enabled",
                            lambda: True)
        q = DelegationQueue(tmp_path / "delegation.db")
        job_id = q.enqueue("任务")
        job = q._claim_next()
        q._finish(job_id, True, result="成功结果")
        row = q._conn.execute(
            "SELECT status, result FROM delegate_queue WHERE id=?",
            (job_id,)).fetchone()
        assert row[0] == "done"
        assert row[1] == "成功结果"


class TestDelegationWorker:
    def test_worker_executes_jobs(self, tmp_path, monkeypatch):
        """常驻 worker 逐条执行入队任务，结果落 done。"""
        monkeypatch.setattr("src.agent.async_delegation.delegate_backend_enabled",
                            lambda: True)
        q = DelegationQueue(tmp_path / "delegation.db")
        done = []

        def executor(job):
            done.append(job["task"])
            return f"ok:{job['task']}"

        worker = DelegationWorker(q, executor)
        job_id = q.enqueue("任务甲")
        job_id2 = q.enqueue("任务乙")
        worker.start()
        deadline = time.time() + 15
        while time.time() < deadline and len(done) < 2:
            time.sleep(0.2)
        worker.stop()
        assert sorted(done) == ["任务乙", "任务甲"]
        assert q.pending_count() == 0
        for jid in (job_id, job_id2):
            row = q._conn.execute(
                "SELECT status FROM delegate_queue WHERE id=?", (jid,)).fetchone()
            assert row[0] == "done"

    def test_worker_retries_failure(self, tmp_path, monkeypatch):
        """worker 对失败任务自动排期重试（首次失败后仍 pending 可再领）。"""
        monkeypatch.setattr("src.agent.async_delegation.delegate_backend_enabled",
                            lambda: True)
        q = DelegationQueue(tmp_path / "delegation.db")
        calls = {"n": 0}

        def flaky(job):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("首次失败")
            return "二次成功"

        worker = DelegationWorker(q, flaky)
        job_id = q.enqueue("易失败任务")
        worker.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            row = q._conn.execute(
                "SELECT status FROM delegate_queue WHERE id=?",
                (job_id,)).fetchone()
            if row and row[0] == "done":
                break
            time.sleep(0.2)
        worker.stop()
        row = q._conn.execute(
            "SELECT status, attempts FROM delegate_queue WHERE id=?",
            (job_id,)).fetchone()
        assert row[0] == "done"
        assert row[1] >= 2
