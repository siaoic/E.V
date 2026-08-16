"""全局异常体系单元测试：错误码枚举 + 基础异常行为。"""
import pytest

from src.core.exceptions import ErrorCode, EVBaseException


class TestErrorCode:
    def test_success_zero(self):
        assert ErrorCode.SUCCESS == 0

    def test_external_service_range(self):
        # 1xxx 外部服务异常
        assert 1000 < ErrorCode.LLM_CONNECT_FAILED < 2000
        assert 1000 < ErrorCode.MCP_TOOL_FAILED < 2000

    def test_param_range(self):
        # 4xxx 参数 / 输入错误
        assert 4000 < ErrorCode.INVALID_EVENT_DATA < 5000
        assert 4000 < ErrorCode.TOOL_NOT_FOUND < 5000

    def test_internal_range(self):
        assert ErrorCode.INTERNAL_ERROR == 5000


class TestEVBaseException:
    def test_message_preserved(self):
        """消息文本原样保留，不追加前缀（异常行为 100% 不变）。"""
        exc = EVBaseException(ErrorCode.LLM_CONNECT_FAILED, "连接超时")
        assert exc.msg == "连接超时"
        assert str(exc) == "连接超时"

    def test_code_attached(self):
        exc = EVBaseException(ErrorCode.TOOL_NOT_FOUND, "工具不存在")
        assert exc.code == ErrorCode.TOOL_NOT_FOUND

    def test_is_exception(self):
        assert issubclass(EVBaseException, Exception)

    def test_catchable_as_exception(self):
        with pytest.raises(EVBaseException) as ei:
            raise EVBaseException(ErrorCode.INTERNAL_ERROR, "内部错误")
        assert ei.value.code == ErrorCode.INTERNAL_ERROR
