"""E.V 5.0 Command Line Interface（真实实现层）。

调用入口：
    python -m src.cli run --profile demo     （通过 src/cli.py forward 层）
    python -m ev.cli run --profile demo      （直接真实入口）
    ev run --profile demo                    （pip install -e 后 entry_points）

Profile / plugins / run 实现与 src/cli.py 旧版字节等价，仅内部 import
从 src.* 改为 ev.*，顶层 src.utils.config 不改（forward 层保证一致）。
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

# --- 可选依赖 stub：mindcraft.bridge 需要 socketio；缺包时不影响 kernel 路径的 ev run --profile demo ---
try:
    import socketio  # noqa: F401  # 安装了则真实使用
except ModuleNotFoundError:
    class _StubSocketIO:
        class AsyncClient:
            async def connect(self, *a, **k): pass
            async def disconnect(self): pass
            def on(self, *a, **k): pass
            async def wait(self): pass
    sys.modules["socketio"] = _StubSocketIO()

# --- 顶部工具（避免循环 import）---
def _project_root() -> Path:
    """cfg.PROJECT_ROOT；cfg 失败时取 ev/cli.py 的祖父目录（项目根）"""
    try:
        from ev.utils.config import cfg
        return Path(cfg.PROJECT_ROOT)
    except Exception:
        return Path(__file__).resolve().parent.parent   # ev/cli.py → ev → project


def _user_plugins_dir() -> Path:
    """~/.ev/plugins"""
    d = Path(os.path.expanduser("~/.ev/plugins"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profiles_dir() -> Path:
    """<PROJECT_ROOT>/profiles"""
    d = _project_root() / "profiles"
    return d

# ============================================================
# 1. 子命令: profiles list
# ============================================================
def cmd_profiles_list(args) -> int:
    prof_dir = _profiles_dir()
    if not prof_dir.is_dir():
        print(f"[profiles] 目录不存在: {prof_dir}")
        return 1
    yamls = sorted(p.name for p in prof_dir.iterdir() if p.is_file() and p.suffix in (".yaml", ".yml"))
    if not yamls:
        print("(无 profile)")
        return 0
    print(f"Profiles ({len(yamls)}):")
    for y in yamls:
        print(f"  - {y}")
    return 0

# ============================================================
# 2. 子命令: plugin add/list/remove
# ============================================================
def _builtin_plugins_dir() -> Path:
    return _project_root() / "plugins" / "builtin"


def _collect_builtin_plugins() -> dict[str, Path]:
    d = _builtin_plugins_dir()
    if not d.is_dir():
        return {}
    result: dict[str, Path] = {}
    for child in sorted(d.iterdir()):
        if child.is_dir() and (child / "metadata.json").is_file():
            result[child.name] = child
    return result


def _collect_user_plugins() -> dict[str, Path]:
    d = _user_plugins_dir()
    result: dict[str, Path] = {}
    if not d.is_dir():
        return result
    for child in sorted(d.iterdir()):
        if child.is_dir() and (child / "metadata.json").is_file():
            result[child.name] = child
    return result


def cmd_plugin_add(args) -> int:
    """ev plugin add <name>。"""
    name: str = args.name
    builtins = _collect_builtin_plugins()
    user_dir = _user_plugins_dir()

    # (1) builtin
    if name in builtins:
        src = builtins[name]
        dst = user_dir / name
        if dst.exists():
            print(f"[plugin] {name} 已存在（用户目录 {dst}），跳过。可用 remove 后再安装。")
            return 0
        try:
            shutil.copytree(src, dst)
        except Exception as e:
            print(f"[plugin] copy 失败: {e}")
            return 3
        md = json.loads((dst / "metadata.json").read_text(encoding="utf-8"))
        v = md.get("version", "?")
        desc = md.get("description", "")
        print(f"[plugin] 已安装 {name}@{v} → {dst}")
        if desc:
            print(f"  描述: {desc}")
        return 0

    # (2) pypi-like
    if "==" in name or name.startswith("ev-plugin-") or (getattr(args, "source", "auto") == "pypi"):
        print(f"[plugin][stub] PyPI 安装（Task17 待接真实 pip subprocess），建议命令：")
        print(f"    pip install {name} -t ~/.ev/plugins/_pypi")
        print(f"[plugin] 当前跳过实际 pip 调用，占位返回 0")
        return 0

    # (3) git-like
    src_plg = getattr(args, "source", "auto")
    if (src_plg == "git") or name.startswith("http") or "@" in name or name.endswith(".git"):
        print(f"[plugin][stub] Git 安装（Task17 待接真实 git clone subprocess），建议命令：")
        print(f"    git clone {name} ~/.ev/plugins/_git/{name.split('/')[-1].replace('.git','')}")
        print(f"[plugin] 当前跳过实际 git clone 调用，占位返回 0")
        return 0

    # (4) 找不到
    print(f"[plugin] 找不到内建或远程形式的插件: {name}")
    print(f"  已注册内建插件: {', '.join(sorted(builtins.keys())) if builtins else '(无)'}")
    return 2


def cmd_plugin_list(args) -> int:
    builtins = _collect_builtin_plugins()
    user = _collect_user_plugins()
    all_names = sorted(set(builtins) | set(user))
    if not all_names:
        print("(无插件)")
        return 0
    print(f"Plugins ({len(all_names)}):")
    for n in all_names:
        locs = []
        if n in builtins:
            locs.append("builtin")
        if n in user:
            locs.append("user")
        loc_str = "+".join(locs)
        d = user.get(n) or builtins.get(n)
        v = "?"
        try:
            v = json.loads((d / "metadata.json").read_text(encoding="utf-8")).get("version", "?")
        except Exception:
            pass
        print(f"  - {n}@{v}  [{loc_str}]")
    return 0


def cmd_plugin_remove(args) -> int:
    name = args.name
    user = _collect_user_plugins()
    if name not in user:
        print(f"[plugin] {name} 未在用户目录 {_user_plugins_dir()} 中安装，跳过。（内建插件不可 remove，只能 add）")
        return 0
    target = user[name]
    try:
        shutil.rmtree(target)
    except Exception as e:
        print(f"[plugin] remove 失败: {e}")
        return 3
    print(f"[plugin] 已移除 {name}（目录 {target}）")
    return 0

# ============================================================
# 3. 子命令: run --profile <name>
# ============================================================
async def _run_arun(profile_name: str, session_dir: str | None = None) -> int:
    """async runner for run 子命令。"""
    from ev.kernel.kernel import Kernel
    from ev.kernel.runtime import RuntimeContext

    # (1) profile 定位
    prof_dir = _profiles_dir()
    candidates = [prof_dir / f"{profile_name}.yaml", prof_dir / f"{profile_name}.yml", Path(profile_name)]
    prof_path: Optional[Path] = None
    for c in candidates:
        if c.is_file():
            prof_path = c
            break
    if prof_path is None:
        print(f"[run] 找不到 profile: {profile_name}（已查 {prof_dir} 与当前目录）")
        return 2
    builtins_root = str(prof_dir)

    # (2) Kernel + RuntimeContext（传 session_dir 作为 data_root，让 SessionLog 写那里）
    kernel = Kernel(str(prof_path), builtins_root=builtins_root, data_root=session_dir)

    class _Cfg:
        RUN_MODE = "cli"
        LLM_MODEL = "cli-dummy"
        PROJECT_ROOT = str(_project_root())
        def __getattr__(self, name): return None

    rt = RuntimeContext(_Cfg(), kernel=kernel)
    await rt.setup()

    brain = rt.brain
    session_log = kernel.session_log
    if brain is None:
        print("[run] 警告：SlotName.model 未激活，无法交互。当前 Kernel 已初始化完毕，退出。")
        await rt.kernel.shutdown()
        return 0

    # (3) 主循环（加 T19 埋点）
    print(f"[run] profile={profile_name} 已加载。输入你的文本（输入 q 退出）:")
    session_file_shown = False
    while True:
        try:
            line = input("[EV] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in ("q", "quit", "exit"):
            break
        try:
            session_log.append("user_input", {"text": line, "proactive": False})
        except Exception as _e:
            print(f"[run][warn] 写入 user_input log 失败: {_e}")
        collected: list[str] = []
        try:
            # chat_stream 新协议：delta 直接打印（打字机），final 累加到 collected
            printed_len = 0
            async for item in brain.chat_stream(line):
                if isinstance(item, tuple) and item and item[0] == "delta":
                    delta = item[1]
                    if len(delta) > printed_len:
                        print(delta[printed_len:], end="", flush=True)
                        printed_len = len(delta)
                elif isinstance(item, tuple) and item and item[0] == "final":
                    print(item[1][printed_len:], end="", flush=True)
                    printed_len = 0
                    collected.append(item[1])
                elif isinstance(item, str):
                    print(item[printed_len:], end="", flush=True)
                    printed_len = 0
                    collected.append(item)
            print()
        except Exception as e:
            print(f"[run] 生成异常: {e}")
            collected = [f"[EV ERROR: {e}]"]
        full_reply = "".join(collected)
        try:
            session_log.append("assistant_output", {
                "reply": full_reply,
                "model": getattr(brain, "name", "?"),
            })
        except Exception as _e:
            print(f"[run][warn] 写入 assistant_output log 失败: {_e}")
        if not session_file_shown:
            try:
                for attr in ("_cur_path", "_file_path", "_cur_file"):
                    p = getattr(session_log, attr, None)
                    if isinstance(p, str) and p:
                        print(f"[run] SessionLog → {p}")
                        session_file_shown = True
                        break
                if not session_file_shown:
                    sid = getattr(session_log, "_session_id", None)
                    if sid:
                        base = session_dir or (getattr(session_log, "_data_root", None) or "./data")
                        p = Path(base) / "sessions" / f"{sid}.jsonl"
                        if p.is_file():
                            print(f"[run] SessionLog → {p.resolve()}")
                            session_file_shown = True
            except Exception:
                pass

    # (4) shutdown
    try:
        session_log.flush()
    except Exception:
        pass
    try:
        await kernel.shutdown()
    except Exception:
        pass
    try:
        sid = getattr(session_log, "_session_id", None)
        if sid:
            base = session_dir or (getattr(session_log, "_data_root", None) or "./data")
            p = Path(base) / "sessions" / f"{sid}.jsonl"
            if p.is_file():
                print(f"[run] 会话日志：{p.resolve()}")
    except Exception:
        pass
    print("[run] bye")
    return 0


def cmd_run(args) -> int:
    try:
        return asyncio.run(_run_arun(args.profile, args.session_dir))
    except KeyboardInterrupt:
        print()
        return 0

# ============================================================
# 4. main：argparse 分发
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ev", description="E.V 5.0 命令行")
    sub = p.add_subparsers(dest="cmd", required=False)

    # ev run
    pr = sub.add_parser("run", help="以指定 profile 启动 EV")
    pr.add_argument("--profile", default="demo", help="profile 名（不带 .yaml）或绝对路径")
    pr.add_argument("--session-dir", default=None, help="SessionLog 输出目录（默认为系统 DATA_ROOT/sessions）")
    pr.set_defaults(func=cmd_run)

    # ev profiles list
    pp = sub.add_parser("profiles", help="profile 管理")
    pp_sub = pp.add_subparsers(dest="profiles_cmd", required=True)
    ppl = pp_sub.add_parser("list", help="列出可用 profile")
    ppl.set_defaults(func=cmd_profiles_list)

    # ev plugin add/list/remove
    pp2 = sub.add_parser("plugin", help="插件管理")
    pp2_sub = pp2.add_subparsers(dest="plugin_cmd", required=True)
    p_add = pp2_sub.add_parser("add", help="安装插件（内建→copy，pypi/git→stub）")
    p_add.add_argument("name")
    p_add.add_argument("--source", choices=["auto", "builtin", "pypi", "git"], default="auto")
    p_add.set_defaults(func=cmd_plugin_add)

    p_list = pp2_sub.add_parser("list", help="列出内建 + 已安装插件")
    p_list.set_defaults(func=cmd_plugin_list)

    p_rm = pp2_sub.add_parser("remove", help="从 ~/.ev/plugins 删除用户插件")
    p_rm.add_argument("name")
    p_rm.set_defaults(func=cmd_plugin_remove)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
