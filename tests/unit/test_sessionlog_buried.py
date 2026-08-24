"""T19 SessionLog 埋点：ev run 结束后 JSONL 含 user_input + assistant_output 两条。"""
from __future__ import annotations
import os
import json
import subprocess
import sys
import textwrap
import types
from pathlib import Path
import pytest

ROOT = str(Path(__file__).resolve().parents[2])
PY = sys.executable
ENV = {**os.environ}
_SUBPROCESS_PATHS = os.pathsep.join([
    ROOT,
    str(Path(ROOT) / "tools"),
])

def _ensure_sitecustomize(tmpdir: Path) -> Path:
    sitecustomize = tmpdir / "sitecustomize.py"
    sitecustomize.write_text(textwrap.dedent(f"""\
        import os, sys, types as _t
        for _p in {[ROOT, str(Path(ROOT) / "tools")]}:
            if _p not in sys.path:
                sys.path.insert(0, _p)
        if "socketio" not in sys.modules:
            _mod = _t.ModuleType("socketio")
            class AsyncClient: pass
            _mod.AsyncClient = AsyncClient
            sys.modules["socketio"] = _mod
        os.environ.setdefault("MEMORY_ENABLED", "false")
    """), encoding="utf-8")
    return tmpdir


def _run(*args, extra_env=None, input_text=None, timeout=120, cwd=ROOT,
         _stub_root: Path | None = None):
    env = {**ENV}
    existing_pp = env.get("PYTHONPATH", "")
    pp_parts = ([str(_stub_root)] if _stub_root else [])
    if _SUBPROCESS_PATHS:
        pp_parts.append(_SUBPROCESS_PATHS)
    if existing_pp:
        pp_parts.append(existing_pp)
    env["PYTHONPATH"] = os.pathsep.join(pp_parts)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        [PY, "-m", "ev.cli", *args],
        cwd=cwd, capture_output=True, text=True,
        env=env, input=input_text, timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


@pytest.fixture
def iso_home(tmp_path, monkeypatch):
    d = tmp_path / "home"; d.mkdir()
    sess = tmp_path / "sessions"; sess.mkdir()
    stub_root = tmp_path / "stub_site"; stub_root.mkdir()
    _ensure_sitecustomize(stub_root)
    extra = {"HOME": str(d), "USERPROFILE": str(d)}
    # monkeypatch 防御
    if "socketio" not in sys.modules:
        _m = types.ModuleType("socketio")
        class _AC: pass
        _m.AsyncClient = _AC
        sys.modules["socketio"] = _m
    return extra, sess, stub_root


def test_t19_sessionlog_has_user_and_assistant(iso_home):
    env_up, sess_dir, stub_root = iso_home
    rc, out, err = _run(
        "run", "--profile", "demo", "--session-dir", str(sess_dir),
        extra_env=env_up, input_text="你好吗\nq\n",
        _stub_root=stub_root,
    )
    combined = out + err
    # 交互成功
    assert "[Echo Demo] 你好吗" in combined, f"echo 未命中: {combined[:1500]}"
    # session log 文件：sess_dir/sessions/*.jsonl 应该恰好一个且含 2 条类型
    log_dir = sess_dir / "sessions"
    assert log_dir.is_dir(), f"sessions dir 不存在: {list(sess_dir.iterdir())}"
    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1, f"jsonl 文件数量异常: {[f.name for f in files]}"
    content = files[0].read_text(encoding="utf-8").splitlines()
    records = [json.loads(ln) for ln in content if ln.strip()]
    types = [r["type"] for r in records]
    assert "user_input" in types, f"缺 user_input 类型: {types}; records={records}"
    assert "assistant_output" in types, f"缺 assistant_output 类型: {types}"
    # user_input 的 text == 你好吗
    u = next(r for r in records if r["type"] == "user_input")
    assert u["payload"]["text"] == "你好吗"
    # assistant_output 的 reply 含 [Echo Demo]
    a = next(r for r in records if r["type"] == "assistant_output")
    assert "[Echo Demo]" in a["payload"]["reply"]


def test_t19_sessionlog_quit_without_interaction(iso_home):
    """只输入 q 立即退出 → session log 里没有用户记录也 OK（不崩）；
    但 SessionLog 至少成功初始化（文件可能不存在也没关系，只要不抛）"""
    env_up, sess_dir, stub_root = iso_home
    rc, out, err = _run(
        "run", "--profile", "demo", "--session-dir", str(sess_dir),
        extra_env=env_up, input_text="q\n",
        _stub_root=stub_root,
    )
    assert rc == 0 or "bye" in (out + err), f"rc={rc} out={out[:500]}"
    # 不抛异常即可；jsonl 可能不存在也符合"没 append 过"
