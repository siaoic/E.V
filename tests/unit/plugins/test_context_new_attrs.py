import pytest
import asyncio
from plugins.context import PluginContext, ConfigView


class FakeSlots:
    def __init__(self):
        self.called = False

    def register(self, *a, **k):
        self.called = True


class FakeJobs:
    def __init__(self):
        self.done = False

    def every(self, s):
        class B:
            def do(slf, fn):
                self.done = True

        return B()


class FakeSessionLog:
    def __init__(self):
        self.items = []

    def append(self, t, p, c=None):
        self.items.append((t, p, c))


class FakeKernel:
    def __init__(self):
        self.slots = FakeSlots()
        self.jobs = FakeJobs()
        self.session_log = FakeSessionLog()
        self._profile_result = {
            "plugin_config": {
                "my_plugin": {"foo": "bar", "num": 42},
            },
        }

    @property
    def profile(self):
        return self._profile_result


class FakeManagerWithKernel:
    def __init__(self):
        self.kernel = FakeKernel()
        self.app = None  # 不会用到 get_config 的旧路径也没关系
        self._subcommands = {}


class FakeManagerNoKernel:
    def __init__(self):
        self.app = None  # 但旧方法 get_plugin_config() 等会用 app.cfg，这里用 None 但只测新属性，不碰旧方法


# --- TR 5.1: 有 Kernel 的 PluginContext 4 个属性可用 ---
def test_with_kernel_attrs_accessible():
    mgr = FakeManagerWithKernel()
    ctx = PluginContext(mgr, "tmp", "my_plugin")
    # slots
    assert ctx.slots is not None and isinstance(ctx.slots, FakeSlots)
    ctx.slots.register("x")
    assert ctx.slots.called is True
    # jobs
    assert ctx.jobs is not None and isinstance(ctx.jobs, FakeJobs)

    async def _fn():
        pass

    ctx.jobs.every(30).do(_fn)
    assert ctx.jobs.done is True
    # session
    assert ctx.session is not None and isinstance(ctx.session, FakeSessionLog)
    ctx.session.append("hello", {"k": 1})
    assert ctx.session.items[0] == ("hello", {"k": 1}, None)
    # config
    assert isinstance(ctx.config, ConfigView)
    assert ctx.config["foo"] == "bar"
    assert ctx.config.get("num") == 42
    # register_subcommand
    async def _h(t):
        return True, "ok"

    ctx.register_subcommand("demo", _h, "demo help")
    assert "demo" in mgr._subcommands
    assert mgr._subcommands["demo"]["help"] == "demo help"


# --- TR 5.2: 无 Kernel（4.x 路径）的旧兼容性：4 属性不崩，返回合理空值；旧 17 方法能访问 ---
def test_no_kernel_compat():
    mgr = FakeManagerNoKernel()
    ctx = PluginContext(mgr, "tmp", "old_plugin")
    # 新属性：返回 None / 空 ConfigView，不抛
    assert ctx.slots is None
    assert ctx.jobs is None
    assert ctx.session is None
    cfg = ctx.config
    assert isinstance(cfg, ConfigView)
    assert len(cfg) == 0
    with pytest.raises(KeyError) as excinfo:
        _ = cfg["missing"]
    assert "missing" in str(excinfo.value) and "可用键" in str(excinfo.value)
    # config.get with default
    assert cfg.get("x", "DEFAULT") == "DEFAULT"
    # 旧方法：log / get_plugin_config 不崩
    ctx.log("info", "hello")  # 只是控制台输出，不要断言
    assert ctx.get_plugin_config() == ctx._plugin_config  # 都是 {} 或 dict，断言相等即可


# --- TR 5.3: ConfigView KeyError 友好提示 & get ---
def test_config_view_behavior():
    cv = ConfigView({"a": 1, "b": 2})
    assert len(cv) == 2
    assert list(cv.keys()) == ["a", "b"]
    assert "a" in cv and "c" not in cv
    with pytest.raises(KeyError) as excinfo:
        _ = cv["z"]
    msg = str(excinfo.value)
    assert "z" in msg and "a" in msg and "b" in msg  # 包含可用键
    assert cv.get("a") == 1
    assert cv.get("x") is None
    assert cv.get("x", 99) == 99
