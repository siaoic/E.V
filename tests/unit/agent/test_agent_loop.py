"""ReActAgent 循环单元测试：规划/执行/观察/压缩/兜底解析。

FakeLLM 返回预定义响应，ToolExecutor 挂 echo 工具，全程不触网。
"""
import json
import pytest
from types import SimpleNamespace

from src.agent.budget import TokenBudget
from src.agent.executor import ToolExecutor
from src.agent.loop import AgentStep, ReActAgent
from src.agent.sandbox import Sandbox


# ---------- LLM 客户端替身 ----------

def _make_resp(content=None, tool_calls=None, usage=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=usage,
    )


def _make_tool_call(name, arguments):
    return SimpleNamespace(
        id="1",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


class FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        return self._owner.responses.pop(0)


class FakeLLM:
    """按顺序消费 responses 的假 LLM；记录每次调用的 kwargs。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=FakeCompletions(self))

    async def close(self):
        pass


# ---------- 工具执行器 ----------

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


def make_agent(root, responses, max_steps=5, budget=1000):
    sandbox, executor = make_executor(root)
    return ReActAgent(
        llm_client=FakeLLM(responses), llm_model="test",
        executor=executor, sandbox=sandbox,
        budget=TokenBudget(budget), max_steps=max_steps,
    ), sandbox


class TestSingleStepFinish:
    @pytest.mark.asyncio
    async def test_finish_returns_result(self, tmp_path):
        llm_resp = _make_resp(content="任务完成", usage=SimpleNamespace(total_tokens=5))
        agent, _ = make_agent(tmp_path, [llm_resp])
        result = await agent.run("打个招呼")
        assert result == "任务完成"

    @pytest.mark.asyncio
    async def test_finish_with_empty_content(self, tmp_path):
        llm_resp = _make_resp(content="  ", usage=SimpleNamespace(total_tokens=5))
        agent, _ = make_agent(tmp_path, [llm_resp])
        assert (await agent.run("测试")) == "（无输出）"

    @pytest.mark.asyncio
    async def test_tools_passed_to_llm(self, tmp_path):
        llm_resp = _make_resp(content="完成", usage=SimpleNamespace(total_tokens=5))
        agent, _ = make_agent(tmp_path, [llm_resp])
        await agent.run("测试")
        tools = agent._llm.calls[0]["tools"]
        assert any(t["function"]["name"] == "echo" for t in tools)


@pytest.mark.asyncio
class TestToolCallPath:
    async def test_tool_then_finish(self, tmp_path):
        agent, sandbox = make_agent(tmp_path, [
            _make_resp(content="调用工具", tool_calls=[_make_tool_call("echo", {"text": "hi"})],
                       usage=SimpleNamespace(total_tokens=5)),
            _make_resp(content="最终结果", usage=SimpleNamespace(total_tokens=5)),
        ])
        result = await agent.run("测试")
        assert result == "最终结果"
        # 两步已消费全部响应
        assert agent._llm.responses == []
        # 观察文本写进历史
        assert "hi" in agent._history[0].observation

    async def test_unknown_tool_returns_observation(self, tmp_path):
        agent, _ = make_agent(tmp_path, [
            _make_resp(content="", tool_calls=[_make_tool_call("no_such_tool", {})],
                       usage=SimpleNamespace(total_tokens=5)),
            _make_resp(content="收尾", usage=SimpleNamespace(total_tokens=5)),
        ])
        result = await agent.run("测试")
        assert result == "收尾"
        assert "未知工具" in agent._history[0].observation

    async def test_max_steps_reached(self, tmp_path):
        tool = _make_tool_call("echo", {"text": "x"})
        responses = [_make_resp(content="", tool_calls=[tool], usage=SimpleNamespace(total_tokens=1))] * 3
        agent, _ = make_agent(tmp_path, responses, max_steps=2)
        result = await agent.run("测试")
        assert "达到最大步数" in result

    async def test_budget_consumed_from_usage(self, tmp_path):
        llm_resp = _make_resp(content="完成", usage=SimpleNamespace(total_tokens=42))
        agent, _ = make_agent(tmp_path, [llm_resp])
        await agent.run("测试")
        assert agent._budget.used == 42

    async def test_llm_error_falls_back_finish(self, tmp_path):
        class Boom:
            def __init__(self):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **kw: (_ for _ in ()).throw(RuntimeError("net"))))
        sandbox, executor = make_executor(tmp_path)
        agent = ReActAgent(
            llm_client=Boom(), llm_model="test", executor=executor,
            sandbox=sandbox, budget=TokenBudget(1000), max_steps=3,
        )
        result = await agent.run("测试")
        assert "LLM 调用失败" in result


class TestTextFallback:
    """tool_calls=None 时从纯文本 JSON 兜底解析（兼容 GLM-4-flash 等）。"""

    @pytest.mark.asyncio
    async def test_tool_call_from_json_block(self, tmp_path):
        agent, _ = make_agent(tmp_path, [
            _make_resp(content='```json\n{"name": "echo", "arguments": {"text": "hi"}}\n```',
                       usage=SimpleNamespace(total_tokens=5)),
            _make_resp(content="完成", usage=SimpleNamespace(total_tokens=5)),
        ])
        result = await agent.run("测试")
        assert result == "完成"
        assert agent._history[0].action["name"] == "echo"

    @pytest.mark.asyncio
    async def test_finish_from_json_block(self, tmp_path):
        agent, _ = make_agent(tmp_path, [
            _make_resp(content='{"action": "finish", "result": "搞定"}',
                       usage=SimpleNamespace(total_tokens=5)),
        ])
        assert (await agent.run("测试")) == "搞定"

    def test_extract_tool_call_json_fence(self):
        text = '思考...\n```json\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'
        r = ReActAgent._extract_tool_call(text)
        assert r["action"] == "tool"
        assert r["tool_call"]["name"] == "read_file"
        assert r["tool_call"]["arguments"] == {"path": "a.txt"}

    def test_extract_tool_call_aliases(self):
        r = ReActAgent._extract_tool_call('{"tool": "echo", "args": {"text": "hi"}}')
        assert r["tool_call"]["name"] == "echo"
        assert r["tool_call"]["arguments"] == {"text": "hi"}

    def test_extract_tool_call_string_args(self):
        r = ReActAgent._extract_tool_call('{"name": "echo", "arguments": "{\\"text\\": \\"hi\\"}"}')
        assert r["tool_call"]["arguments"] == {"text": "hi"}

    def test_extract_tool_call_none(self):
        assert ReActAgent._extract_tool_call("纯文本没有 JSON") is None


class TestHistoryCompress:
    def test_compress_keeps_last_three_plus_summary(self, tmp_path):
        agent, _ = make_agent(tmp_path, [])
        for i in range(5):
            agent._history.append(AgentStep(
                plan="p" * 50,
                action={"name": "echo", "arguments": {"text": "x"}},
                observation="o" * 200,
            ))
        agent._compress_history()
        assert len(agent._history) == 4  # 3 保留 + 1 摘要
        assert agent._history[0].plan == "早期步骤摘要"
        assert "共 2 步" in agent._history[0].observation

    def test_compress_short_history_untouched(self, tmp_path):
        agent, _ = make_agent(tmp_path, [])
        for i in range(3):
            agent._history.append(AgentStep(plan="p", action={"name": "x", "arguments": {}}, observation="o"))
        agent._compress_history()
        assert len(agent._history) == 3

    def test_estimate_tokens(self, tmp_path):
        agent, _ = make_agent(tmp_path, [])
        agent._history.append(AgentStep(
            plan="a" * 40, action={"name": "x", "arguments": {}}, observation="b" * 40))
        # 80 字符 ÷ 2.5 字符/token = 32
        assert agent._estimate_tokens() == 32

    @pytest.mark.asyncio
    async def test_budget_full_triggers_compress(self, tmp_path):
        # 预算极小（3 token，触发线 int(3*0.75)=2）：每步消耗 1 token（usage），
        # 加上历史字符估算立即过线，第 4 步历史超 3 步时真正发生压缩
        tool = _make_tool_call("echo", {"text": "x"})
        responses = [_make_resp(content="", tool_calls=[tool],
                                usage=SimpleNamespace(total_tokens=1))] * 5
        agent, _ = make_agent(tmp_path, responses, max_steps=5, budget=3)
        await agent.run("测试")
        # 触发过压缩后，历史第一条是摘要
        assert agent._history[0].plan == "早期步骤摘要"


@pytest.mark.asyncio
class TestOutputLock:
    async def test_lock_released_after_run(self, tmp_path):
        from src.core.output_lock import get_global_state, get_output_owner, STATE_IDLE
        agent, _ = make_agent(tmp_path, [_make_resp(content="完成", usage=SimpleNamespace(total_tokens=1))])
        await agent.run("测试")
        assert get_output_owner() is None
        assert get_global_state() == STATE_IDLE
