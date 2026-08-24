"""get_weather 工具注册（L3-C）：查询指定城市当前天气。"""

from plugins.builtin.tools.weather import _get_weather


def register(ctx):
    """注册 get_weather：依赖 OpenWeatherMap key，未配置 key 不可用。"""
    ctx.tools.register(
        name="get_weather",
        description="查询指定城市的当前天气。",
        parameters={
            "city": {
                "type": "string",
                "description": "城市名，如：北京、上海",
                "required": True,
            },
        },
        execute=lambda args: _get_weather(**args),
        enabled_by="TOOL_GET_WEATHER_ENABLED",
        requires="OPENWEATHERMAP_API_KEY",
    )
