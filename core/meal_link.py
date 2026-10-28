"""只读【兔吃了么】菜单，失败时安静降级为普通日常。"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from astrbot.api import logger

_SLOT_LABELS = (
    ("breakfast", "早饭"),
    ("lunch", "午饭"),
    ("dinner", "晚饭"),
    ("snack", "加餐"),
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COMPLEX_RE = re.compile(r"[+＋]|炖|煮|烧|炒|蒸|烤|煲|焖|煎|炸|腌|卤|熬")


class MealLink:
    def __init__(self, data_dir: Path | None, config: object = None) -> None:
        self.data_dir = data_dir
        self.config = config
        self._cache_key: tuple[str, float] | None = None
        self._cache_value: dict[str, str] | None = None
        self._warned_date: str | None = None

    def _path(self) -> Path | None:
        raw = ""
        try:
            if self.config is not None:
                raw = str(self.config.get("meal_history_path", "") or "").strip()
        except Exception:
            raw = ""
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute() and self.data_dir is not None:
                path = self.data_dir.parent / path
            return path
        if self.data_dir is None:
            return None
        return self.data_dir.parent / "xavier_daily_meal" / "history.json"

    @staticmethod
    def _clean(value: object) -> str:
        if not isinstance(value, str):
            return ""
        text = value.replace("\r", " ").replace("\n", " ")
        text = _CONTROL_RE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:40]

    def load(self, target: date | str) -> dict[str, str] | None:
        """读取指定日期菜单；任何异常都返回 None，不向外抛。"""
        date_key = target.isoformat() if isinstance(target, date) else str(target)
        path = self._path()
        if path is None:
            return None
        try:
            stat = path.stat()
            cache_key = (str(path), stat.st_mtime)
            if self._cache_key == cache_key and self._cache_value is not None:
                return self._cache_value or None
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("menu history is not an object")
            day = raw.get(date_key)
            if not isinstance(day, dict):
                raise ValueError("today menu is missing")
            cleaned = {
                key: self._clean(day.get(key))
                for key, _ in _SLOT_LABELS
            }
            if not any(cleaned.values()):
                raise ValueError("today menu is empty")
            self._cache_key = cache_key
            self._cache_value = cleaned
            self._warned_date = None
            return cleaned
        except Exception as exc:
            if self._warned_date != date_key:
                logger.warning(
                    f"[DynamicLifeState] 读取【兔吃了么】菜单失败，"
                    f"本次按普通日常生成：{exc}"
                )
                self._warned_date = date_key
            self._cache_key = None
            self._cache_value = None
            return None

    @staticmethod
    def _looks_complex(value: str) -> bool:
        return bool(value and (_COMPLEX_RE.search(value) or len(value) >= 8))

    def section(self, period: str, target: date | str) -> str:
        """返回提示词里的一小段菜单上下文；读不到时返回普通日常口径。"""
        if not self._enabled():
            return "无"
        menu = self.load(target)
        if not menu:
            return (
                "今天没有可用的菜单。涉及吃饭时只写「吃了点东西」「随便对付了一口」"
                "这类中性说法，不要编具体菜名。"
            )

        if period == "上午":
            dish = menu.get("breakfast", "")
            label = "早饭"
        elif period == "中午":
            dish = menu.get("lunch", "")
            label = "午饭"
        elif period == "下午":
            dish = menu.get("dinner", "")
            label = "晚饭"
            if not self._looks_complex(dish):
                return "无"
        else:
            dish = menu.get("dinner", "")
            label = "晚饭"

        if not dish:
            return (
                f"{label}没有安排。不要编具体菜名，写成随便吃了点即可。"
            )
        return (
            f"{label}菜单：{dish}。\n"
            "只把其中一样当作饭点附近的生活锚点，最多提一个菜名，不要报菜名；\n"
            "如果这道菜需要买菜备料，可以用一句话带过前序，不要写做菜全过程。\n"
            "summary 仍然只写一句 18~32 字的场景，不要让菜单撑满整句。"
        )

    def _enabled(self) -> bool:
        try:
            return bool(self.config.get("meal_link_enable", True))
        except Exception:
            return True
