"""get_weather 工具：OpenWeatherMap Geocoding 定位 + One Call 3.0 取天气。"""

import httpx

from src.utils import config

_WEATHER_GEO_URL = "https://api.openweathermap.org/geo/1.0/direct"
_WEATHER_ONECALL_URL = "https://api.openweathermap.org/data/3.0/onecall"


async def _get_weather(city: str) -> str:
    """OpenWeatherMap：Geocoding 定位 + One Call 3.0 取天气。"""
    key = config.cfg.OPENWEATHERMAP_API_KEY
    if not key:
        return "错误：未配置 OPENWEATHERMAP_API_KEY，无法查询天气。请在 .env 中填入。"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1) 城市 → 经纬度
            geo_resp = await client.get(
                _WEATHER_GEO_URL,
                params={"q": city, "limit": 1, "appid": key},
            )
            geo_resp.raise_for_status()
            locations = geo_resp.json()
            if not locations:
                return f"错误：未找到城市「{city}」，请检查城市名是否正确。"
            lat = locations[0]["lat"]
            lon = locations[0]["lon"]
            display_name = locations[0].get("local_names", {}).get("zh", city)

            # 2) One Call 3.0
            weather_resp = await client.get(
                _WEATHER_ONECALL_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "exclude": "minutely,hourly,daily,alerts",
                    "units": "metric",
                    "lang": "zh_cn",
                    "appid": key,
                },
            )
            weather_resp.raise_for_status()
            data = weather_resp.json()
    except Exception as e:
        return f"错误：天气查询失败（{e}）。"

    current = data.get("current") or {}
    temp = current.get("temp")
    feels_like = current.get("feels_like")
    humidity = current.get("humidity")
    wind = (current.get("wind_speed") or 0) * 3.6  # m/s → km/h
    desc = (current.get("weather") or [{}])[0].get("description", "未知")

    return (
        f"{display_name}当前天气：{desc}，气温 {temp:.1f}℃，体感 {feels_like:.1f}℃，"
        f"湿度 {humidity}%，风速约 {wind:.0f} 公里每小时。"
    )
