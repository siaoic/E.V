"""ToolExecutor 参数规整单元测试：schema 过滤 + 别名映射 + 字符串参数。

覆盖 LLM 产生幻觉参数（未在 schema 中声明、或用了别名）时的兜底行为。
"""
import json

import pytest

from ev.agent.executor import ToolExecutor
from ev.agent.sandbox import Sandbox


def _echo(sandbox, **kwargs):
    return json.dumps(kwargs, ensure_ascii=False)


ECHO_SCHEMA = {
    "name": "echo", "description": "原样返回参数",
    "parameters": {"type": "object",
                   "properties": {"text": {"type": "string"}},
                   "required": ["text"]},
}


def _make_executor(root):
    sandbox = Sandbox(root=str(root))
    tools = {"echo": (ECHO_SCHEMA, _echo)}
    return sandbox, ToolExecutor(tools, sandbox)


@pytest.mark.asyncio
class TestSchemaFilter:
    async def test_extra_keys_dropped(self, tmp_path):
        """LLM 幻觉的多余参数被丢弃，不触发 TypeError。"""
        _, executor = _make_executor(tmp_path)
        out = await executor.execute("echo", {"text": "hi", "mode": "force", "verbose": True})
        assert json.loads(out) == {"text": "hi"}

    async def test_required_missing_still_reports(self, tmp_path):
        """必要参数缺失仍走 TypeError 提示路径（过滤后为空参 → 具名必填参数报错）。"""
        sandbox = Sandbox(root=str(tmp_path))
        from ev.agent.tools import build_builtin_tools
        executor = ToolExecutor(build_builtin_tools(), sandbox)
        out = await executor.execute("write_file", {"mode": "force"})
        assert "参数错误" in out
        assert "path" in out and "content" in out


@pytest.mark.asyncio
class TestAliasMapping:
    async def test_filepath_maps_to_path(self, tmp_path):
        """write_file 幻觉 filepath 参数 → 映射为 path。"""
        sandbox = Sandbox(root=str(tmp_path))
        from ev.agent.tools import build_builtin_tools
        executor = ToolExecutor(build_builtin_tools(), sandbox)
        out = await executor.execute("write_file", {"filepath": "a.txt", "content": "hello"})
        assert "已写入" in out
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"

    async def test_alias_not_override_existing(self, tmp_path):
        """规范参数已提供时，别名键不覆盖。"""
        sandbox = Sandbox(root=str(tmp_path))
        from ev.agent.tools import build_builtin_tools
        executor = ToolExecutor(build_builtin_tools(), sandbox)
        out = await executor.execute("write_file", {"path": "a.txt", "filepath": "b.txt", "content": "x"})
        assert "已写入" in out
        assert (tmp_path / "a.txt").exists()
        assert not (tmp_path / "b.txt").exists()

    async def test_alias_target_not_in_schema_dropped(self, tmp_path):
        """别名目标不在 schema 中时整键丢弃。"""
        _, executor = _make_executor(tmp_path)
        out = await executor.execute("echo", {"text": "hi", "cmd": "ls"})
        assert json.loads(out) == {"text": "hi"}


@pytest.mark.asyncio
class TestStringArgs:
    async def test_args_as_json_string(self, tmp_path):
        """args 为 JSON 字符串时先解析再执行。"""
        _, executor = _make_executor(tmp_path)
        out = await executor.execute("echo", '{"text": "hi"}')
        assert json.loads(out) == {"text": "hi"}

    async def test_invalid_json_string_reports(self, tmp_path):
        """非法 JSON 字符串给出参数错误提示。"""
        _, executor = _make_executor(tmp_path)
        out = await executor.execute("echo", "{not json")
        assert "参数错误" in out
