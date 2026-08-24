"""辅助 LLM 路由与记账（3.16）单测：路由覆盖 / 记账落盘 / fail-open。"""
import asyncio
import json

from ev.llm import auxiliary


def _run(coro):
    return asyncio.run(coro)


class TestModelRouting:
    def test_default_uses_main_model(self, monkeypatch):
        # LLM_MODEL/AUX_MODELS 是 cfg property，需 patch 底层子配置字段
        monkeypatch.setattr("ev.utils.config.cfg.llm.LLM_MODEL", "主模型A")
        monkeypatch.setattr("ev.utils.config.cfg.llm.AUX_MODELS", {})
        assert auxiliary.get_aux_model("butler.extract") == "主模型A"

    def test_override_by_task(self, monkeypatch):
        monkeypatch.setattr("ev.utils.config.cfg.llm.LLM_MODEL", "主模型A")
        monkeypatch.setattr("ev.utils.config.cfg.llm.AUX_MODELS",
                            {"review": "glm-4-flash"})
        assert auxiliary.get_aux_model("review") == "glm-4-flash"
        # 未配置任务仍回主模型
        assert auxiliary.get_aux_model("topic.gen") == "主模型A"


class TestRecordAuxUsage:
    def test_records_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "_USAGE_PATH",
                            str(tmp_path / "aux_usage.jsonl"))
        auxiliary.record_aux_usage("butler.extract", "m1", 100, 50, 1.5)
        lines = (tmp_path / "aux_usage.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["task"] == "butler.extract"
        assert rec["model"] == "m1"
        assert rec["prompt_tokens"] == 100
        assert rec["completion_tokens"] == 50

    def test_excluded_tasks_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "_USAGE_PATH",
                            str(tmp_path / "aux_usage.jsonl"))
        auxiliary.record_aux_usage("review", "m1", 100, 50, 1.0)
        assert not (tmp_path / "aux_usage.jsonl").exists()

    def test_disabled_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "_USAGE_PATH",
                            str(tmp_path / "aux_usage.jsonl"))
        monkeypatch.setattr(auxiliary, "aux_accounting_enabled", lambda: False)
        auxiliary.record_aux_usage("butler.extract", "m1", 1, 1, 0.1)
        assert not (tmp_path / "aux_usage.jsonl").exists()

    def test_empty_task_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "_USAGE_PATH",
                            str(tmp_path / "aux_usage.jsonl"))
        auxiliary.record_aux_usage("", "m1", 1, 1, 0.1)
        assert not (tmp_path / "aux_usage.jsonl").exists()

    def test_summary_aggregates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "_USAGE_PATH",
                            str(tmp_path / "aux_usage.jsonl"))
        auxiliary.record_aux_usage("a.task", "m", 10, 5, 0.2)
        auxiliary.record_aux_usage("a.task", "m", 20, 10, 0.4)
        auxiliary.record_aux_usage("b.task", "m", 30, 15, 0.6)
        summary = auxiliary.get_aux_usage_summary()
        assert "2 次" in summary or "2次" in summary
        assert "a.task" in summary and "b.task" in summary

    def test_summary_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "_USAGE_PATH",
                            str(tmp_path / "empty_aux_usage.jsonl"))
        assert "暂无" in auxiliary.get_aux_usage_summary()


class _FakeResponse:
    def __init__(self, content="你好", prompt=10, completion=5):
        self.choices = [type("Choice", (), {"message": type(
            "Msg", (), {"content": content, "reasoning_content": None})})]
        self.usage = type("Usage", (), {
            "prompt_tokens": prompt, "completion_tokens": completion})


class _FakeClient:
    def __init__(self, resp=None, error=None):
        self._resp = resp
        self._error = error
        self.closed = False

    async def chat_completions_create(self, **kwargs):
        if self._error is not None:
            raise self._error
        return self._resp

    @property
    def chat(self):
        return type("Chat", (), {"completions": type(
            "Completions", (),
            {"create": self.chat_completions_create})})()

    async def close(self):
        self.closed = True


class TestCallLlm:
    def test_success_returns_text_and_usage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "_USAGE_PATH",
                            str(tmp_path / "aux_usage.jsonl"))
        monkeypatch.setattr("ev.utils.config.cfg.llm.LLM_MODEL", "主模型A")
        fake = _FakeClient(_FakeResponse("你好呀", 100, 50))
        monkeypatch.setattr(
            "ev.llm.client.factory.get_async_openai_client",
            lambda **kw: fake)

        text, usage = _run(auxiliary.call_llm(
            "topic.gen", "你是助手", [{"role": "user", "content": "hi"}]))
        assert text == "你好呀"
        assert usage["model"] == "主模型A"
        assert usage["prompt_tokens"] == 100
        # 记账已落盘
        assert (tmp_path / "aux_usage.jsonl").exists()
        assert not fake.closed  # 池化客户端不关闭（由 pool 统一管理，共享连接池）

    def test_failure_fail_open(self, monkeypatch):
        monkeypatch.setattr("ev.utils.config.cfg.llm.LLM_MODEL", "主模型A")

        class _Boom(Exception):
            pass

        fake = _FakeClient(error=_Boom("网络故障"))
        monkeypatch.setattr(
            "ev.llm.client.factory.get_async_openai_client",
            lambda **kw: fake)
        text, usage = _run(auxiliary.call_llm("topic.gen", "你是助手", []))
        assert text is None
        assert usage["task"] == "topic.gen"

    def test_no_model_fails_early(self, monkeypatch):
        monkeypatch.setattr("ev.utils.config.cfg.llm.LLM_MODEL", "")
        monkeypatch.setattr("ev.utils.config.cfg.llm.AUX_MODELS", {})
        text, usage = _run(auxiliary.call_llm("topic.gen", "你是助手", []))
        assert text is None
        assert usage["model"] == ""
