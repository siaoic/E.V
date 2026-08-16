"""统一错误处理测试：异常 → ErrorEvent → EV_ERROR 广播。"""
import asyncio

from src.core.bus import EV_ERROR, bus
from src.core.error_handler import report_error
from src.core.exceptions import ErrorCode, EVBaseException


class TestReportError:
    def test_plain_exception_maps_internal(self):
        async def _inner():
            got = []
            async def handler(ev):
                got.append(ev)
            bus.subscribe(EV_ERROR, handler)
            try:
                exc = RuntimeError("boom")
                ev = await report_error(exc, msg="对话流程出错：boom")
                assert ev.code == ErrorCode.INTERNAL_ERROR.value
                assert ev.code_name == "INTERNAL_ERROR"
                assert ev.msg == "对话流程出错：boom"
                # 广播事件与返回值是同一个对象
                assert got == [ev]
            finally:
                bus.unsubscribe(EV_ERROR, handler)

        asyncio.run(_inner())

    def test_msg_defaults_to_exc_text(self):
        async def _inner():
            ev = await report_error(ValueError("bad"))
            assert ev.msg == "bad"

        asyncio.run(_inner())

    def test_ev_base_exception_keeps_own_code(self):
        async def _inner():
            exc = EVBaseException(ErrorCode.LLM_CONNECT_FAILED, "连接失败")
            ev = await report_error(exc)
            assert ev.code == ErrorCode.LLM_CONNECT_FAILED.value
            assert ev.code_name == "LLM_CONNECT_FAILED"

        asyncio.run(_inner())

    def test_explicit_code_overrides(self):
        async def _inner():
            ev = await report_error(RuntimeError("x"), code=ErrorCode.TTS_TIMEOUT.value)
            assert ev.code == ErrorCode.TTS_TIMEOUT.value
            assert ev.code_name == "TTS_TIMEOUT"

        asyncio.run(_inner())
