"""ev.kernel 包：运行时骨架（RuntimeContext）+ 组合式 component setup/teardown。

RuntimeContext 通过 __getattr__ 懒加载，避免 ev.kernel.__init__ 触发
ev.kernel._helpers → ev.llm.stream → ev.tts.engine 的循环导入。
"""

__all__ = ["RuntimeContext"]


def __getattr__(name):
    if name == "RuntimeContext":
        from .runtime import RuntimeContext
        return RuntimeContext
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
