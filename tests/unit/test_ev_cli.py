"""T17 (plugin add/list/remove) + T18 (run/profiles list/--help) CLI 端到端。"""
from __future__ import annotations
import subprocess
import sys
import os
import json
import textwrap
import types
import pytest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])   # tests/unit → project
PY = sys.executable
ENV = {**os.environ}
# 把项目根 / tools 同步到 subprocess 的 PYTHONPATH（对齐 tests/conftest.py）
_SUBPROCESS_PATHS = os.pathsep.join([
    ROOT,
    str(Path(ROOT) / "tools"),
])

def _ensure_sitecustomize(tmpdir: Path) -> Path:
    """在 tmpdir 下创建 sitecustomize.py：
       - 补齐 sys.path（项目根 / tools）
       - 给缺少的 socketio 模块打最小 stub（避免 ev.mindcraft.bridge 导入失败）
       - 关闭 MEMORY_ENABLED 等测试开关
    返回可放到 PYTHONPATH 首位的目录。
    """
    sitecustomize = tmpdir / "sitecustomize.py"
    sitecustomize.write_text(textwrap.dedent(f"""\
        import os, sys, types as _t
        # 1) 补齐 sys.path（对齐 conftest）
        for _p in {[ROOT, str(Path(ROOT) / "tools")]}:
            if _p not in sys.path:
                sys.path.insert(0, _p)
        # 2) socketio stub（沙箱经常缺 python-socketio）
        if "socketio" not in sys.modules:
            _mod = _t.ModuleType("socketio")
            class AsyncClient: pass
            _mod.AsyncClient = AsyncClient
            sys.modules["socketio"] = _mod
        # 3) 测试环境开关
        os.environ.setdefault("MEMORY_ENABLED", "false")
    """), encoding="utf-8")
    return tmpdir


def _run(*args, extra_env=None, input_text=None, timeout=120, cwd=ROOT,
         _stub_root: Path | None = None):
    env = {**ENV}
    # 把 ROOT / tools 加到子进程 PYTHONPATH
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
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
        timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


@pytest.fixture
def home(tmp_path, monkeypatch):
    """把 ~/.ev/plugins 定位到临时目录；并返回用于 subprocess 的 sitecustomize 根。"""
    d = tmp_path / "home"
    d.mkdir()
    stub_root = tmp_path / "stub_site"
    stub_root.mkdir()
    _ensure_sitecustomize(stub_root)
    # 不同平台 expanduser 依赖 HOME(Posix) / USERPROFILE(Win)
    extra = {"HOME": str(d), "USERPROFILE": str(d)}
    # monkeypatch 测试函数自己导入 runtime.py 时也能成功（作为防御）
    if "socketio" not in sys.modules:
        _m = types.ModuleType("socketio")
        class _AC: pass
        _m.AsyncClient = _AC
        sys.modules["socketio"] = _m
    return d, extra, stub_root


# --- T18.1: ev --help → 包含 "usage:" + "run" + "profiles" + "plugin"
def test_t18_cli_help(home):
    _, extra, stub_root = home
    rc, out, err = _run("--help", extra_env=extra, _stub_root=stub_root)
    assert rc == 0
    assert "usage:" in out or "usage:" in err
    combined = out + err
    for kw in ("run", "profiles", "plugin"):
        assert kw in combined, f"缺少关键词 {kw}: {combined[:200]}"

# --- T18.2: ev profiles list → 列出 demo/pet/live_bili/live_dy/mcp_sandbox/full
def test_t18_profiles_list(home):
    _, extra, stub_root = home
    rc, out, err = _run("profiles", "list", extra_env=extra, _stub_root=stub_root)
    assert rc == 0
    for n in ("demo.yaml", "pet.yaml", "live_bili.yaml", "live_dy.yaml", "mcp_sandbox.yaml", "full.yaml"):
        assert n in out, f"缺少 profile {n}: {out[:300]}"

# --- T17.1: ev plugin list → 列出 tts_edge / echo_llm / avatar_xiaoyuanzi_local ... 至少 echo_llm
def test_t17_plugin_list_shows_builtin(home):
    _, extra, stub_root = home
    rc, out, err = _run("plugin", "list", extra_env=extra, _stub_root=stub_root)
    assert rc == 0
    assert "echo_llm" in out, f"echo_llm 不在 list 输出: {out[:300]}"
    assert "[builtin]" in out, out[:200]

# --- T17.2: ev plugin add echo_llm → 拷贝到 tmp ~/.ev/plugins/echo_llm
def test_t17_plugin_add_copy(home):
    hd, extra, stub_root = home
    rc, out, err = _run("plugin", "add", "echo_llm", extra_env=extra, _stub_root=stub_root)
    assert rc == 0, f"rc={rc} out={out} err={err}"
    # 目录含 metadata.json
    md = Path(hd) / ".ev" / "plugins" / "echo_llm" / "metadata.json"
    assert md.is_file(), f"{md} 不存在"
    info = json.loads(md.read_text(encoding="utf-8"))
    assert info["name"] == "echo_llm"
    # plugin list 输出包含 [builtin+user]
    rc, out, err = _run("plugin", "list", extra_env=extra, _stub_root=stub_root)
    assert "builtin+user" in out, out[:300]

# --- T17.3: ev plugin remove echo_llm → 目录消失
def test_t17_plugin_remove(home):
    hd, extra, stub_root = home
    # 先 add
    _run("plugin", "add", "echo_llm", extra_env=extra, _stub_root=stub_root)
    rc, out, err = _run("plugin", "remove", "echo_llm", extra_env=extra, _stub_root=stub_root)
    assert rc == 0
    assert not (Path(hd) / ".ev" / "plugins" / "echo_llm").exists()
    # 再 remove 一次不崩（返回 0，提示未安装）
    rc, out, _ = _run("plugin", "remove", "echo_llm", extra_env=extra, _stub_root=stub_root)
    assert rc == 0

# --- T18.3: ev run --profile demo 输入 "你好" + "q" → 输出 "[Echo Demo] 你好"
def test_t18_run_demo_profile_echo(home):
    hd, extra, stub_root = home
    rc, out, err = _run("run", "--profile", "demo", extra_env=extra,
                        input_text="你好\nq\n", _stub_root=stub_root)
    combined = out + err
    assert rc == 0 or "bye" in combined, f"rc={rc} out={out[:500]} err={err[:500]}"
    assert "[Echo Demo] 你好" in combined, f"未命中 echo 结果: {combined[:1500]}"

# --- T17.4: ev plugin add <pypi-pkg> → stub 输出不崩（含 pip install ... 提示）
def test_t17_plugin_pypi_stub(home):
    _, extra, stub_root = home
    rc, out, err = _run("plugin", "add", "ev-plugin-thing==1.0.0",
                        extra_env=extra, _stub_root=stub_root)
    assert rc == 0, f"rc={rc} out={out} err={err}"
    assert "pip install" in (out + err)

# --- T18.4: ev run --profile missing → 返回码非零，说明找不到 profile
def test_t18_run_profile_not_found(home):
    _, extra, stub_root = home
    rc, out, err = _run("run", "--profile", "definitely-not-exist-xyz",
                        extra_env=extra, input_text="q\n", _stub_root=stub_root)
    assert rc != 0 or ("找不到" in (out + err)), f"rc={rc} out={out} err={err}"
