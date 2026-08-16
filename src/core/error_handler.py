"""统一错误处理：异常 → ErrorEvent → 总线广播。

收敛各处手写 ErrorEvent 构造与 EV_ERROR 广播的重复代码：
- EVBaseException 携带自有错误码，其余异常映射 INTERNAL_ERROR；
- msg 与调用方传入保持一致（不追加前缀，保证广播载荷与改造前相同）；
- 调用方统一用 `await report_error(e, msg="...")` 上报。
"""

from __future__ import annotations

from typing import Optional


async def report_error(exc: BaseException, *, msg: Optional[str] = None,
                       code: Optional[int] = None) -> object:
    """统一异常上报：构造 ErrorEvent 并广播 EV_ERROR，返回事件供调用方复用。

    code 优先级：显式传入 > EVBaseException 自带错误码 > INTERNAL_ERROR。
    """
    from src.core.bus import EV_ERROR, bus
    from src.core.events.models import ErrorEvent
    from src.core.exceptions import EVBaseException, ErrorCode

    if code is None:
        if isinstance(exc, EVBaseException):
            code = exc.code.value
        else:
            code = ErrorCode.INTERNAL_ERROR.value
    try:
        code_name = ErrorCode(code).name
    except ValueError:
        code_name = str(code)
    error = ErrorEvent(
        code=code,
        code_name=code_name,
        msg=msg if msg is not None else str(exc),
    )
    await bus.emit(EV_ERROR, error)
    return error
