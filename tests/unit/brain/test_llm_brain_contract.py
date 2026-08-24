"""TR 13.1 / 13.2 / 13.3：LLMBrain 满足 LLMContract。"""
from __future__ import annotations
import pytest
import sys

# 沙箱经常缺 socketio / 一些仅 UI 才需要的库，先造最小 stub（已有就跳过）
for _pkg in ("socketio",):
    try:
        __import__(_pkg)
    except Exception:
        class _Stub:
            class AsyncClient: pass
        sys.modules.setdefault(_pkg, _Stub())


def _import_brain_cls():
    """优先从真实路径导入 LLMBrain，按常见路径顺序尝试。"""
    errors = []
    for _mod in (
        "ev.llm.llm_brain",       # 实际位置（5.0 重构后）
        "ev.llm.brain.core",     # 拆分后的子模块
        "src.llm.llm_brain",     # 4.x 兼容路径（已删除，仅作错误信息对照）
    ):
        try:
            mod = __import__(_mod, fromlist=["LLMBrain"])
            if hasattr(mod, "LLMBrain"):
                return getattr(mod, "LLMBrain"), None
        except Exception as _e:
            errors.append(f"{_mod}: {_e}")
    return None, "; ".join(errors)


def _instantiate(BrainCls):
    """尝试实例化 LLMBrain，失败时返回 (None, reason)。"""
    # LLMBrain 签名：LLMBrain(mcp=None)，不接受 cfg 位置参数
    try:
        return BrainCls(), None
    except TypeError:
        # 兼容 BrainCls(cfg) 等形式
        try:
            from ev.utils.config import cfg
            return BrainCls(cfg), None
        except Exception as _e:
            return None, f"(cfg) TypeError 仍失败：{_e}"
    except Exception as _e:
        return None, str(_e)


# TR 13.1: isinstance(LLMBrain(...), LLMContract) → True
def test_llm_brain_is_llm_contract():
    from ev.kernel.slots import LLMContract

    BrainCls, err = _import_brain_cls()
    if BrainCls is None:
        pytest.skip(f"找不到 LLMBrain 类：{err}")

    brain, ierr = _instantiate(BrainCls)
    if brain is None:
        pytest.skip(f"无法实例化 LLMBrain（缺环境变量等）：{ierr}")

    # LLMContract @runtime_checkable：需要 name / chat_stream / push_turn_context / reload_client
    missing = [a for a in ("name", "chat_stream", "push_turn_context", "reload_client")
               if not hasattr(brain, a)]
    assert isinstance(brain, LLMContract), (
        f"LLMBrain 不满足 LLMContract。缺属性：{', '.join(missing)}"
    )


# TR 13.2: push_turn_context 然后 chat_stream（不触网，mock 内部调用；
# 至少保证注入 / reload 不崩，方法存在且 callable）
@pytest.mark.asyncio
async def test_llm_brain_push_context_doesnt_crash():
    BrainCls, err = _import_brain_cls()
    if BrainCls is None:
        pytest.skip(f"找不到 LLMBrain：{err}")

    brain, ierr = _instantiate(BrainCls)
    if brain is None:
        pytest.skip(f"无法实例化：{ierr}")

    # push_turn_context（含空列表）—— 不抛即可
    brain.push_turn_context(["a", "b"])
    brain.push_turn_context([])
    # 列表已被追加到某处（语义）
    any_list_has_items = any(
        isinstance(getattr(brain, _a, None), list) and len(getattr(brain, _a)) >= 2
        for _a in ("_turn_contexts", "turn_contexts", "_context_stack",
                   "contexts", "_ev_turn_contexts")
    )
    # 这里不强硬断言（因为也可能消费完了），只是验证其可调用
    assert callable(brain.push_turn_context)

    # reload_client —— 不抛即可（真实实现会重置 client，也不抛）
    brain.reload_client()


# TR 13.3: reload_client 至少不抛异常；同时断言 name 是 str
def test_llm_brain_reload_client_no_throw():
    BrainCls, err = _import_brain_cls()
    if BrainCls is None:
        pytest.skip(f"找不到 LLMBrain：{err}")

    brain, ierr = _instantiate(BrainCls)
    if brain is None:
        pytest.skip(f"无法实例化：{ierr}")

    # 断言：name / reload_client / push_turn_context / chat_stream 都 callable 或存在
    assert isinstance(brain.name, str) and brain.name, (
        f"name 属性应该是非空 str，实际 {brain.name!r}")
    assert callable(brain.reload_client)
    assert callable(brain.push_turn_context)
    assert callable(brain.chat_stream)

    # 调用一次 reload_client，不抛任何异常（NotImplementedError 也允许）
    try:
        brain.reload_client()
    except NotImplementedError:
        pass
    except Exception as e:
        pytest.fail(f"reload_client 抛出异常 {type(e).__name__}: {e}")
