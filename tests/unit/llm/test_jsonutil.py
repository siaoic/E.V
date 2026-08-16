"""LLM JSON 统一容错解析工具测试：围栏/全角/区间截取/类型校验。"""
import pytest

from src.llm.jsonutil import (
    extract_json_text,
    parse_json_array,
    parse_json_object,
)


class TestExtractJsonText:
    def test_strip_fence(self):
        assert extract_json_text('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_strip_fence_no_lang(self):
        assert extract_json_text('```\n[1, 2]\n```') == "[1, 2]"

    def test_fullwidth_conversion(self):
        assert extract_json_text('{"a"：1， "b"：2}') == '{"a":1, "b":2}'
        assert extract_json_text("[１，２]") == "[1,2]"

    def test_none_and_blank(self):
        assert extract_json_text(None) == ""
        assert extract_json_text("  ") == ""


class TestParseJsonObject:
    def test_direct_parse(self):
        assert parse_json_object('{"verdict": "ADD"}') == {"verdict": "ADD"}

    def test_non_dict_returns_empty(self):
        assert parse_json_object("[1, 2]") == {}

    def test_prefix_suffix_trimmed(self):
        assert parse_json_object('说明：{"a": 1} 完成') == {"a": 1}

    def test_nested_braces_kept(self):
        assert parse_json_object('前 {"a": {"b": 1}} 后') == {"a": {"b": 1}}

    def test_garbage_returns_empty(self):
        assert parse_json_object("不是 JSON") == {}

    def test_fence_and_fullwidth_combined(self):
        assert parse_json_object('```json\n{"a"：1}\n```') == {"a": 1}

    def test_direct_parse_gives_priority_to_object(self):
        # 整串是对象时优先返回对象，不因内部含 [] 而误判
        assert parse_json_object('{"k": [1, 2]}') == {"k": [1, 2]}


class TestParseJsonArray:
    def test_direct_parse(self):
        assert parse_json_array('[{"name": "x"}]') == [{"name": "x"}]

    def test_non_array_returns_none(self):
        assert parse_json_array('{"a": 1}') is None

    def test_prefix_suffix_trimmed(self):
        assert parse_json_array('结果如下：[1, 2] 请查收') == [1, 2]

    def test_garbage_returns_none(self):
        assert parse_json_array("没有数组") is None

    def test_fence_and_fullwidth_combined(self):
        assert parse_json_array('```json\n[１，２]\n```') == [1, 2]
