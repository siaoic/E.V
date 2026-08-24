"""Task 7 单元测试：PluginManager 升级（kernel/profile、register(ctx) 新风格、7 个新钩子）。

覆盖 TR 7.1 / 7.2 / 7.3 / 7.4。
"""

import json
import os
import textwrap

import pytest

from plugins.manager import PluginManager


# ======================================================================
# TR 7.1: register(ctx) 风格 mock 插件加载成功，ctx.slots.register 被调
# ======================================================================


@pytest.mark.asyncio
async def test_load_register_style_plugin_populates_slot(tmp_path):
    """TR 7.1: profile 驱动 → register(ctx) 插件 → ctx.slots.register 被调用 1 次。"""
    # 1) tmp_path 下建 fake_plugin/
    d = tmp_path / "fake_plugin"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"name": "fake", "version": "1.0", "main": "reg.py"}),
        encoding="utf-8",
    )

    # 2) 造 FakeKernel + FakeSlots
    class FakeSlots:
        registered = []

        def register(self, slot, name, inst):
            FakeSlots.registered.append((slot, name, inst))

    # 每跑一次测试重置类变量，避免污染
    FakeSlots.registered.clear()

    class FakeKernel:
        def __init__(self):
            self.slots = FakeSlots()

        @property
        def profile(self):
            return {
                "plugins": {
                    "builtin": ["fake_plugin"],
                    "pypi": [],
                    "git": [],
                },
                "slots": {},
                "plugin_config": {},
            }

    kernel = FakeKernel()

    # 3) 写 reg.py（register 风格：优先被命中并 return，不走 Plugin 子类逻辑）
    reg_code = textwrap.dedent("""
def register(ctx):
    if ctx.slots is not None:
        ctx.slots.register("slot_llm", "fake_impl", {"hello": "world"})
""")
    (d / "reg.py").write_text(reg_code, encoding="utf-8")

    # 4) 构造带 kernel 的 PluginManager，load_all 应走 profile 路径
    pm = PluginManager(app=None, plugins_dir=str(tmp_path), kernel=kernel)
    await pm.load_all()

    # 断言 slots.register 被调用
    assert ("slot_llm", "fake_impl", {"hello": "world"}) in FakeSlots.registered
    # 断言插件已登记到 _plugins（register 风格：plugin 为 Plugin 子类占位，带 register_style 标记）
    assert "fake" in pm._plugins
    entry = pm._plugins["fake"]
    from plugins.base import Plugin
    assert entry.get("plugin") is not None and isinstance(entry["plugin"], Plugin)
    assert entry.get("register_style") == "function"


# ======================================================================
# TR 7.2: Plugin 基类风格（旧风格）：on_init / on_start 调用顺序
# ======================================================================


@pytest.mark.asyncio
async def test_load_plugin_style_lifecycle(tmp_path):
    """TR 7.2: 无 kernel → 旧路径（enabled_plugins.json 自动登记）。Plugin 子类 on_init → on_start 顺序。"""
    d = tmp_path / "myp"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"name": "p", "main": "index.py"}),
        encoding="utf-8",
    )

    mark_file = tmp_path / "seq.txt"
    mark_path_str = str(mark_file).replace("\\", "\\\\")
    code = textwrap.dedent(f'''
from plugins.base import Plugin
import os
MARK_PATH = r"{mark_path_str}"
class P(Plugin):
    async def on_init(self):
        if MARK_PATH:
            with open(MARK_PATH, "a", encoding="utf-8") as f:
                f.write("init\\n")
    async def on_start(self):
        if MARK_PATH:
            with open(MARK_PATH, "a", encoding="utf-8") as f:
                f.write("start\\n")
''')
    (d / "index.py").write_text(code, encoding="utf-8")

    # 无 kernel → 走旧路径：自动登记 enabled_plugins.json（tmp_path 下新文件）
    pm = PluginManager(app=None, plugins_dir=str(tmp_path), kernel=None)
    await pm.load_all()          # load → on_init
    await pm.start_all()         # on_start

    data = mark_file.read_text(encoding="utf-8").splitlines() if mark_file.exists() else []
    assert "init" in data, f"on_init 未写入，当前: {data}"
    assert "start" in data, f"on_start 未写入，当前: {data}"
    assert data.index("init") < data.index("start"), f"顺序异常: {data}"


# ======================================================================
# TR 7.3: 无 kernel 时 load_all 行为 ≡ 旧路径（公共函数读写一致）
# ======================================================================


def test_load_enabled_compat(tmp_path):
    """TR 7.3: load_enabled_plugins / save_plugin_sets 公共函数行为与 4.x 完全一致。"""
    from plugins.manager import load_enabled_plugins, save_plugin_sets

    # 写 enabled_plugins.json
    save_plugin_sets(str(tmp_path), ["a", "b"], ["c"])
    enabled = load_enabled_plugins(str(tmp_path))
    assert enabled == {"a", "b"}

    # 文件结构校验（旧格式兼容：plugins + disabled 字段）
    data = json.loads((tmp_path / "enabled_plugins.json").read_text(encoding="utf-8"))
    assert data.get("plugins") == ["a", "b"]
    assert data.get("disabled") == ["c"]


# ======================================================================
# TR 7.4: dispatch on_slot_activate → 注册了钩子的 Plugin 基类插件 handler 被调用 1 次
# ======================================================================


@pytest.mark.asyncio
async def test_dispatch_new_hook_slot_activate(tmp_path):
    """TR 7.4: 通用 dispatch_hook 能分发 7 个新钩子之一（on_slot_activate），Plugin 子类方法被调用 1 次。"""
    d = tmp_path / "hp"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"name": "hp", "main": "index.py"}),
        encoding="utf-8",
    )
    cnt_path = tmp_path / "cnt.txt"
    cnt_str = str(cnt_path).replace("\\", "\\\\")
    code = textwrap.dedent(f'''
from plugins.base import Plugin
import os
p = r"{cnt_str}"
class HP(Plugin):
    async def on_slot_activate(self, event):
        n = 0
        try:
            with open(p, "r") as f:
                n = int(f.read() or "0")
        except Exception:
            pass
        with open(p, "w") as f:
            f.write(str(n + 1))
''')
    (d / "index.py").write_text(code, encoding="utf-8")

    pm = PluginManager(app=None, plugins_dir=str(tmp_path), kernel=None)
    await pm.load_all()
    await pm.start_all()

    # 调 pm.dispatch_hook("on_slot_activate", ev)
    from plugins.base import SlotActivateEvent
    ev = SlotActivateEvent("model", None, {"x": 1})
    await pm.dispatch_hook("on_slot_activate", ev)

    # 计数器应为 1
    assert cnt_path.exists(), "on_slot_activate 未写入计数文件"
    assert cnt_path.read_text(encoding="utf-8") == "1", (
        f"on_slot_activate 调用次数不对，实际: {cnt_path.read_text(encoding='utf-8')}")
