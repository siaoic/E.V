"""记忆生命周期判决链单元测试：ADD / UPDATE / DELETE / IGNORE 与容错回退。

LifecycleEngine 依赖 LLM 判决与相似记忆召回，二者都通过替身注入，
不触网、不依赖真实配置。
"""
from types import SimpleNamespace

import pytest

from ev.llm.memory.lifecycle import LifecycleEngine


# ---------- 替身 ----------

class FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.last_kwargs = kwargs
        return self._owner.response()


class FakeClient:
    """返回预设 content 的假 OpenAI 客户端；记录最近一次调用参数。"""

    def __init__(self, owner, content):
        self._owner = owner
        owner.response = lambda: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )
        owner.last_kwargs = None
        self.chat = SimpleNamespace(completions=FakeCompletions(owner))


def make_engine(recall_result, content):
    """构造带 fake client 的引擎；recall_result 为 recall_similar 的返回值。"""
    async def recall(content, owner, top_k):
        return recall_result
    engine = LifecycleEngine(recall_similar=recall)
    engine._client = FakeClient(engine, content)
    engine._model = "test"
    return engine


# 相似度 0.7：高于默认阈值 0.6（进入 LLM 判决），低于预筛阈值 0.88
# （不被规则预筛拦下），保证 LLM 判决用例真正走 LLM 路径。
_SIMILAR = [{"id": "1", "content": "用户喜欢咖啡", "similarity": 0.7}]


@pytest.mark.asyncio
class TestEmptyInput:
    async def test_empty_content_ignored(self):
        engine = LifecycleEngine()
        assert await engine.judge("") == ("IGNORE", None)

    async def test_whitespace_content_ignored(self):
        engine = LifecycleEngine()
        assert await engine.judge("   ") == ("IGNORE", None)


@pytest.mark.asyncio
class TestNoSimilar:
    async def test_empty_recall_returns_add(self):
        async def recall(content, owner, top_k):
            return []
        engine = LifecycleEngine(recall_similar=recall)
        assert await engine.judge("用户喜欢喝咖啡") == ("ADD", None)

    async def test_recall_below_threshold_returns_add(self):
        async def recall(content, owner, top_k):
            return [{"id": "1", "content": "x", "similarity": 0.3}]
        engine = LifecycleEngine(recall_similar=recall, threshold=0.6)
        assert await engine.judge("用户喜欢喝咖啡") == ("ADD", None)

    async def test_recall_exception_returns_add(self):
        async def recall(content, owner, top_k):
            raise RuntimeError("boom")
        engine = LifecycleEngine(recall_similar=recall)
        assert await engine.judge("用户喜欢喝咖啡") == ("ADD", None)


@pytest.mark.asyncio
class TestLLMVerdict:
    async def test_verdict_add(self):
        engine = make_engine(_SIMILAR, '{"verdict": "ADD", "target_id": null, "reason": "新"}')
        verdict, tid = await engine.judge("用户喜欢喝咖啡")
        assert verdict == "ADD"
        assert tid is None

    async def test_verdict_ignore(self):
        engine = make_engine(_SIMILAR, '{"verdict": "IGNORE", "target_id": null}')
        assert await engine.judge("用户喜欢喝咖啡") == ("IGNORE", None)

    async def test_update_with_valid_target(self):
        engine = make_engine(_SIMILAR, '{"verdict": "UPDATE", "target_id": "1"}')
        assert await engine.judge("用户喜欢喝咖啡") == ("UPDATE", "1")

    async def test_update_with_int_target(self):
        engine = make_engine(_SIMILAR, '{"verdict": "UPDATE", "target_id": 1}')
        assert await engine.judge("用户喜欢喝咖啡") == ("UPDATE", "1")

    async def test_delete_with_valid_target(self):
        engine = make_engine(_SIMILAR, '{"verdict": "DELETE", "target_id": "1"}')
        assert await engine.judge("用户喜欢喝咖啡") == ("DELETE", "1")

    async def test_update_with_invalid_target_ignored(self):
        engine = make_engine(_SIMILAR, '{"verdict": "UPDATE", "target_id": "999"}')
        assert await engine.judge("用户喜欢喝咖啡") == ("IGNORE", None)

    async def test_update_with_missing_target_ignored(self):
        engine = make_engine(_SIMILAR, '{"verdict": "UPDATE", "target_id": null}')
        assert await engine.judge("用户喜欢喝咖啡") == ("IGNORE", None)

    async def test_invalid_verdict_falls_back_add(self):
        engine = make_engine(_SIMILAR, '{"verdict": "FOO", "target_id": null}')
        assert await engine.judge("用户喜欢喝咖啡") == ("ADD", None)

    async def test_llm_exception_returns_add(self):
        async def recall(content, owner, top_k):
            return _SIMILAR
        engine = LifecycleEngine(recall_similar=recall)
        engine._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kw: (_ for _ in ()).throw(RuntimeError("net")))))
        engine._model = "test"
        assert await engine.judge("用户喜欢喝咖啡") == ("ADD", None)

    async def test_no_client_returns_add(self, monkeypatch):
        async def recall(content, owner, top_k):
            return _SIMILAR
        engine = LifecycleEngine(recall_similar=recall)
        monkeypatch.setattr(engine, "_ensure_client", lambda: None)
        assert await engine.judge("用户喜欢喝咖啡") == ("ADD", None)

    async def test_prompt_contains_fact_and_context(self):
        engine = make_engine(_SIMILAR, '{"verdict": "ADD", "target_id": null}')
        await engine.judge("用户喜欢喝咖啡")
        user_msg = engine._client._owner.last_kwargs["messages"][1]["content"]
        assert "用户喜欢喝咖啡" in user_msg
        assert "用户喜欢咖啡" in user_msg
        assert "test" in engine._client._owner.last_kwargs["model"]


class TestRulePrescreen:
    """_rule_prescreen 规则预筛：命中任一规则直接 IGNORE，否则放行 LLM。"""

    @staticmethod
    def _prescreen(content, similar):
        return LifecycleEngine._rule_prescreen(content, similar)

    def test_exact_same_text_ignored(self):
        similar = [{"id": "1", "content": "用户喜欢咖啡", "similarity": 0.9}]
        assert self._prescreen("用户喜欢咖啡", similar) == "IGNORE"

    def test_exact_same_text_ignored_whitespace_insensitive(self):
        similar = [{"id": "1", "content": "用户喜欢 咖啡", "similarity": 0.9}]
        assert self._prescreen("用户喜欢咖啡", similar) == "IGNORE"

    def test_high_similarity_high_overlap_ignored(self):
        # 0.9 ≥ 0.88 且 token 重叠 6/7 ≈ 0.857 ≥ 0.25 → 近似重复
        similar = [{"id": "1", "content": "用户喜欢咖啡", "similarity": 0.9}]
        assert self._prescreen("用户喜欢喝咖啡", similar) == "IGNORE"

    def test_high_similarity_low_overlap_passes(self):
        # 相似度高但 token 几乎无重叠（不同主题）→ 放行 LLM
        similar = [{"id": "1", "content": "用户喜欢咖啡", "similarity": 0.9}]
        assert self._prescreen("我今天去游乐园玩了一天", similar) is None

    def test_lore_leak_ignored(self):
        similar = [{"id": "1", "content": "用户聊到游戏", "similarity": 0.7}]
        assert self._prescreen("流萤和萨姆是什么关系", similar) == "IGNORE"

    def test_low_similarity_passes(self):
        similar = [{"id": "1", "content": "用户喜欢咖啡", "similarity": 0.7}]
        assert self._prescreen("用户喜欢喝咖啡", similar) is None


class TestExtractJson:
    def test_plain_json(self):
        assert LifecycleEngine._extract_json('{"a": 1}') == '{"a": 1}'

    def test_markdown_fence(self):
        assert LifecycleEngine._extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_noise_around_json(self):
        assert LifecycleEngine._extract_json('返回结果：{"a": 1} 完成') == '{"a": 1}'

    def test_multiple_braces(self):
        assert LifecycleEngine._extract_json('前 {"a": {"b": 1}} 后') == '{"a": {"b": 1}}'
