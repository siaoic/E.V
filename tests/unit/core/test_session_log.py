"""SessionLog 单元测试（TR 2.1 / 2.2 / 2.3 + 稳健性）。"""
import json
import os

from ev.kernel.session_log import SessionLog


def test_append_5条(tmp_path):
    """TR 2.1：append 5 条，行数=5，字段齐全。"""
    log = SessionLog(data_root=str(tmp_path))
    for i in range(5):
        log.append(f"type_{i}", {"v": i}, {"ctx": i})
    entries = log.entries()
    assert len(entries) == 5
    for i, e in enumerate(entries):
        assert "timestamp" in e and isinstance(e["timestamp"], float)
        assert e["type"] == f"type_{i}"
        assert e["context"] == {"ctx": i}
        assert e["payload"] == {"v": i}


def test_new_session_unique(tmp_path):
    """TR 2.2：两次 new_session 生成不同 id，切换后上个会话数据独立。"""
    log = SessionLog(data_root=str(tmp_path))
    id1 = log.new_session()
    log.append("x", {"from": 1})
    id2 = log.new_session()
    assert id1 != id2
    log.append("x", {"from": 2})
    # 当前会话（id2）只有一条
    entries = log.entries()
    assert len(entries) == 1 and entries[0]["payload"]["from"] == 2
    # 手动打开 id1 文件验证
    id1_path = os.path.join(
        os.path.abspath(str(tmp_path)), "sessions", f"{id1}.jsonl"
    )
    assert os.path.exists(id1_path)
    with open(id1_path, "r", encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["payload"]["from"] == 1


def test_entries_fifo(tmp_path):
    """TR 2.3：entries 返回顺序与 append 顺序一致。"""
    log = SessionLog(data_root=str(tmp_path))
    log.append("a", {"n": 1})
    log.append("b", {"n": 2})
    log.append("c", {"n": 3})
    assert [e["payload"]["n"] for e in log.entries()] == [1, 2, 3]


def test_write_after_close(tmp_path):
    """稳健性：close() 后再次 append 自动 new_session 成功。"""
    log = SessionLog(data_root=str(tmp_path))
    log.append("before", {"v": 1})
    sid_before = log._session_id
    assert sid_before is not None
    log.close()
    assert log._session_id is None
    log.append("after", {"v": 2})
    assert log._session_id is not None
    assert log._session_id != sid_before
    entries = log.entries()
    assert len(entries) == 1
    assert entries[0]["payload"] == {"v": 2}
    assert entries[0]["type"] == "after"
