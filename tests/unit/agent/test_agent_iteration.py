"""阶段 3 loop 集成测试：迭代预算宽限收尾 + delegate 子代理阻塞清单。

对标 UPGRADE_PLAN_HERMES.md 3.1 验证节：构造"永不 finish"的假工具，
断言循环在 max_iterations 处收尾并产出总结（总结调用不带工具定义）；
3.8：子代理 executor 不含 delegate / play_sound_effect。
"""
import json
import pytest
from types import SimpleNamespace

from src.agent.budget import TokenBudget
from src.agent.executor import ToolExecutor
from src.agent.loop import ReActAgent
from src.agent.sandbox import Sandbox


def _make_resp(content=None, tool_calls=None, usage=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=usage,
    )


def _make_tool_call(name, arguments):
    return SimpleNamespace(
        id="1",
        function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


class FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        return self._owner.responses.pop(0)


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=FakeCompletions(self))

    async def close(self):
        pass


def _echo(sandbox, **kwargs):
    return json.dumps(kwargs, ensure_ascii=False)


def make_executor(root):
    sandbox = Sandbox(root=str(root))
    tools = {
        "echo": (
            {"name": "echo", "description": "原样返回参数",
             "parameters": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}},
            _echo,
        ),
    }
    return sandbox, ToolExecutor(tools, sandbox)


def _noop_after_run(task, result):
    """跳过任务沉淀（依赖真实 config 额外字段，单测内不触发）。"""
    return None


@pytest.mark.asyncio
class TestIterationBudgetGrace:
    async def test_never_finish_stops_at_budget_with_summary(self, tmp_path):
        """永不 finish 的假工具：预算耗尽后走不带工具定义的总结调用。"""
        tool = _make_tool_call("echo", {"text": "x"})
        responses = [
            _make_resp(content="", tool_calls=[tool], usage=SimpleNamespace(total_tokens=1)),
            _make_resp(content="", tool_calls=[tool], usage=SimpleNamespace(total_tokens=1)),
            _make_resp(content="预算耗尽总结", usage=SimpleNamespace(total_tokens=1)),
        ]
        sandbox, executor = make_executor(tmp_path)
        agent = ReActAgent(
            llm_client=FakeLLM(responses), llm_model="test",
            executor=executor, sandbox=sandbox,
            budget=TokenBudget(1000), max_steps=10, max_iterations=2,
        )
        # 沉淀路径依赖真实 config.cfg 的额外字段，单测内跳过（存量行为，与本项无关）
        agent._after_run = _noop_after_run
        result = await agent.run("测试")
        assert result == "预算耗尽总结"
        # 2 常规轮 + 1 宽限总结轮
        assert len(agent._llm.calls) == 3
        assert agent._iter_budget.used == 2
        # 宽限总结调用不带任何工具定义
        assert agent._llm.calls[-1]["tools"] is None
        # 常规轮带工具定义
        assert agent._llm.calls[0]["tools"] is not None

    async def test_budget_above_steps_keeps_original_behavior(self, tmp_path):
        """max_iterations 大于步数时行为与旧版一致（步数上限先到）。"""
        tool = _make_tool_call("echo", {"text": "x"})
        responses = [
            _make_resp(content="", tool_calls=[tool], usage=SimpleNamespace(total_tokens=1)),
            _make_resp(content="", tool_calls=[tool], usage=SimpleNamespace(total_tokens=1)),
        ]
        sandbox, executor = make_executor(tmp_path)
        agent = ReActAgent(
            llm_client=FakeLLM(responses), llm_model="test",
            executor=executor, sandbox=sandbox,
            budget=TokenBudget(1000), max_steps=2, max_iterations=10,
        )
        result = await agent.run("测试")
        assert "达到最大步数" in result

    async def test_rerun_resets_budget_and_grace(self, tmp_path):
        """同一实例二次 run：迭代预算与宽限标记均复位。"""
        sandbox, executor = make_executor(tmp_path)
        llm = FakeLLM([
            _make_resp(content="第一次", usage=SimpleNamespace(total_tokens=1)),
            _make_resp(content="第二次", usage=SimpleNamespace(total_tokens=1)),
        ])
        agent = ReActAgent(
            llm_client=llm, llm_model="test",
            executor=executor, sandbox=sandbox,
            budget=TokenBudget(1000), max_steps=5, max_iterations=1,
        )
        agent._after_run = _noop_after_run
        assert (await agent.run("任务一")) == "第一次"
        assert agent._iter_budget.used == 1
        assert agent._budget_grace_call is True  # 未耗尽，宽限未用
        assert (await agent.run("任务二")) == "第二次"


@pytest.mark.asyncio
class TestDelegateBlockedTools:
    async def test_subagent_excludes_blocked_tools(self, tmp_path, monkeypatch):
        """子代理 executor 剔除 delegate / play_sound_effect（防递归 + 防输出干扰）。"""
        sandbox, executor = make_executor(tmp_path)
        captured = []
        llm = FakeLLM([_make_resp(content="子任务搞定", usage=SimpleNamespace(total_tokens=1))])
        agent = ReActAgent(
            llm_client=llm, llm_model="test",
            executor=executor, sandbox=sandbox,
            budget=TokenBudget(1000), max_steps=3,
        )
        # 主代理已构造，此刻起 spy 只捕获子代理
        orig_init = ReActAgent.__init__

        def spy(self, *args, **kwargs):
            captured.append(kwargs.get("executor"))
            return orig_init(self, *args, **kwargs)

        monkeypatch.setattr(ReActAgent, "__init__", spy)
        result = await agent._delegate(["完成一个子任务"])
        assert "子任务 1" in result
        assert "子任务搞定" in result
        # 仅捕获到子代理 executor
        assert len(captured) == 1
        names = {t["name"] for t in captured[0].schemas}
        assert "delegate" not in names
        assert "play_sound_effect" not in names
        assert "echo" in names
