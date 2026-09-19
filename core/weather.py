"""天气服务：为生活状态提供当日天气摘要（Open-Meteo，免密钥）。

设计要点：
- 渲染路径不阻塞：结果按城市缓存（内存 + 磁盘），TTL 60 分钟。
- 失败静默降级：网络异常、接口变更、超时都返回 None，绝不影响 /life 输出。
- 城市可配置，默认成都；常见城市内置坐标，避免多一次地理编码请求。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from astrbot.api import logger

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp 为 AstrBot 运行环境内置依赖
    aiohttp = None  # type: ignore[assignment]

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_CITY = "成都"
REQUEST_TIMEOUT = 8
CACHE_TTL = 60 * 60
CACHE_VERSION = 1

# 内置坐标：默认城市与常见城市，命中时省去地理编码请求
BUILTIN_COORDS: dict[str, tuple[float, float]] = {
    "成都": (30.6667, 104.0667),
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "杭州": (30.2741, 120.1551),
    "重庆": (29.5630, 106.5516),
    "武汉": (30.5928, 114.3055),
    "西安": (34.3416, 108.9398),
    "南京": (32.0603, 118.7969),
    "天津": (39.3434, 117.3616),
    "长沙": (28.2282, 112.9388),
    "郑州": (34.7466, 113.6254),
    "青岛": (36.0671, 120.3826),
    "厦门": (24.4798, 118.0894),
    "昆明": (24.8801, 102.8329),
    "沈阳": (41.8057, 123.4315),
}

# WMO weather code -> (emoji, 中文描述)
WMO_TABLE: dict[int, tuple[str, str]] = {
    0: ("☀️", "晴"),
    1: ("🌤️", "晴间多云"),
    2: ("⛅", "多云"),
    3: ("☁️", "阴"),
    45: ("🌫️", "雾"),
    48: ("🌫️", "雾凇"),
    51: ("🌦️", "小毛毛雨"),
    53: ("🌦️", "毛毛雨"),
    55: ("🌦️", "大毛毛雨"),
    56: ("🌧️", "冻毛毛雨"),
    57: ("🌧️", "冻毛毛雨"),
    61: ("🌧️", "小雨"),
    63: ("🌧️", "中雨"),
    65: ("🌧️", "大雨"),
    66: ("🌧️", "冻雨"),
    67: ("🌧️", "冻雨"),
    71: ("🌨️", "小雪"),
    73: ("🌨️", "中雪"),
    75: ("❄️", "大雪"),
    77: ("🌨️", "米雪"),
    80: ("🌦️", "阵雨"),
    81: ("🌧️", "强阵雨"),
    82: ("⛈️", "暴雨"),
    85: ("🌨️", "阵雪"),
    86: ("❄️", "强阵雪"),
    95: ("⛈️", "雷阵雨"),
    96: ("⛈️", "雷阵雨伴冰雹"),
    99: ("⛈️", "强雷暴伴冰雹"),
}

UNKNOWN_WEATHER = ("🌡️", "未知")


def describe_weather(
    code: int, is_day: bool = True, temperature: object = None
) -> tuple[str, str, str]:
    """把 WMO 天气代码、昼夜标记与温度转换成 (emoji, 描述, 温度文本)。"""
    emoji, text = WMO_TABLE.get(code, UNKNOWN_WEATHER)
    if not is_day:
        if code == 0:
            emoji, text = "🌙", "晴"
        elif code == 1:
            emoji = "🌙"
    if isinstance(temperature, (int, float)):
        temp_text = f"{round(float(temperature))}°C"
    else:
        temp_text = "--"
    return emoji, text, temp_text


def format_weather_line(emoji: str, text: str, temp_text: str) -> str:
    """渲染成一行展示文本，例如「☁️ 天气：多云 23°C」。"""
    return f"{emoji} 天气：{text} {temp_text}"


class WeatherService:
    """按城市缓存当日天气，供渲染层调用。"""

    def __init__(self, config, data_dir: Path) -> None:
        self._config = config
        self._cache_file = Path(data_dir) / "weather_cache.json"
        self._lock = asyncio.Lock()
        self._session = None
        self._geo: dict[str, tuple[float, float]] = {}
        self._weather: dict[str, dict] = {}
        self._loaded = False

    # ===== 配置 =====

    def _get_config(self, key: str, default):
        if self._config is None:
            return default
        try:
            if key in self._config:
                return self._config[key]
            getter = getattr(self._config, "get", None)
            if callable(getter):
                value = getter(key, default)
                return default if value is None else value
        except Exception:
            pass
        return default

    @property
    def enabled(self) -> bool:
        return bool(self._get_config("weather_enable", True))

    @property
    def city(self) -> str:
        raw = self._get_config("weather_city", DEFAULT_CITY)
        city = str(raw).strip() if raw is not None else ""
        return city or DEFAULT_CITY

    # ===== 生命周期 =====

    async def initialize(self) -> None:
        self._load_cache()
        self._ensure_session()

    async def terminate(self) -> None:
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                logger.warning("[DynamicLifeState] 天气会话关闭失败", exc_info=True)

    def _ensure_session(self):
        if aiohttp is None:
            return None
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "AstrBot-Plugin-DynamicLifeState/1.0"},
            )
        return self._session

    # ===== 对外接口 =====

    async def get_line(self) -> str | None:
        """返回形如「☁️ 天气：多云 23°C」的文本；不可用时返回 None。"""
        if not self.enabled:
            return None
        try:
            result = await self._get_weather(self.city)
        except Exception:
            logger.exception("[DynamicLifeState] 获取天气失败")
            return None
        if result is None:
            return None
        return format_weather_line(
            str(result.get("emoji", "")),
            str(result.get("text", "")),
            str(result.get("temp", "--")),
        )

    # ===== 内部实现 =====

    async def _get_weather(self, city: str) -> dict | None:
        cached = self._read_memory_cache(city)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._read_memory_cache(city)
            if cached is not None:
                return cached
            result = await self._fetch(city)
            if result is not None:
                self._weather[city] = {**result, "ts": time.time()}
                self._persist_cache()
            return result

    def _read_memory_cache(self, city: str) -> dict | None:
        entry = self._weather.get(city)
        if not isinstance(entry, dict):
            return None
        ts = entry.get("ts")
        if not isinstance(ts, (int, float)):
            return None
        if time.time() - float(ts) > CACHE_TTL:
            return None
        return entry

    async def _fetch(self, city: str) -> dict | None:
        coords = await self._geocode(city)
        if coords is None:
            return None
        lat, lon = coords
        payload = await self._request_json(
            FORECAST_URL,
            {
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "current": "weather_code,temperature_2m,is_day",
                "timezone": "Asia/Shanghai",
            },
        )
        if not payload:
            return None
        current = payload.get("current")
        if not isinstance(current, dict):
            return None
        code = current.get("weather_code")
        if not isinstance(code, (int, float)):
            return None
        is_day = current.get("is_day", 1)
        emoji, text, temp_text = describe_weather(
            int(code), bool(is_day), current.get("temperature_2m")
        )
        return {"emoji": emoji, "text": text, "temp": temp_text}

    async def _geocode(self, city: str) -> tuple[float, float] | None:
        builtin = BUILTIN_COORDS.get(city)
        if builtin is not None:
            return builtin
        cached = self._geo.get(city)
        if cached is not None:
            return cached
        data = await self._request_json(
            GEOCODE_URL,
            {"name": city, "count": 1, "language": "zh", "format": "json"},
        )
        results = (data or {}).get("results")
        if not isinstance(results, list) or not results:
            logger.warning(f"[DynamicLifeState] 未能解析城市坐标：{city}")
            return None
        first = results[0]
        lat, lon = first.get("latitude"), first.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None
        coords = (float(lat), float(lon))
        self._geo[city] = coords
        self._persist_cache()
        return coords

    async def _request_json(self, url: str, params: dict) -> dict | None:
        session = self._ensure_session()
        if session is None:
            logger.warning("[DynamicLifeState] aiohttp 不可用，跳过天气获取")
            return None
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[DynamicLifeState] 天气接口返回 {resp.status}：{url}"
                    )
                    return None
                return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.warning("[DynamicLifeState] 天气接口请求超时")
        except aiohttp.ClientError as e:
            logger.warning(f"[DynamicLifeState] 天气接口请求失败：{e}")
        except Exception:
            logger.exception("[DynamicLifeState] 天气接口解析失败")
        return None

    # ===== 磁盘缓存 =====

    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if not self._cache_file.exists():
                return
            raw = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
                return
            geo = raw.get("geo")
            if isinstance(geo, dict):
                for key, value in geo.items():
                    if (
                        isinstance(value, list)
                        and len(value) == 2
                        and all(isinstance(v, (int, float)) for v in value)
                    ):
                        self._geo[str(key)] = (float(value[0]), float(value[1]))
            weather = raw.get("weather")
            if isinstance(weather, dict):
                for key, value in weather.items():
                    if isinstance(value, dict):
                        self._weather[str(key)] = value
        except Exception:
            logger.warning("[DynamicLifeState] 天气缓存读取失败，已忽略", exc_info=True)

    def _persist_cache(self) -> None:
        payload = {
            "version": CACHE_VERSION,
            "geo": {k: [v[0], v[1]] for k, v in self._geo.items()},
            "weather": self._weather,
        }
        tmp_path = self._cache_file.with_suffix(".tmp")
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp_path, self._cache_file)
        except Exception:
            logger.warning("[DynamicLifeState] 天气缓存写入失败，已忽略", exc_info=True)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
