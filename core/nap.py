"""午睡：一天一次的小睡计划、Otto 代班、以及睡眠数据桥。

设计要点：
- 午睡用「日期 + 固定种子」稳定掷骰，同一天结果不变，重启后也不会变卦。
- 时机：当天睡眠数据就绪（或到兜底线）之后才生成计划，0 点不再预生成。
- 醒来状态（自然醒 / 被叫醒）落到 nap_state.json，午睡分钟数只结算一次。
- 睡眠时长由这里生成：读用户健康数据（Xavier_care 的日记录），
  在此基础上 +30~60 分钟浮动，写成 night_sleep_min。
- 通过 sleep_bridge.json 把「昨夜睡眠 + 午睡加成」交给 Xavier_care 展示，
  对方读不到文件就完全回退它自己的逻辑，不会互相绑死。
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .state import CHINA_TIMEZONE

NAP_FILE = "nap_state.json"
BRIDGE_FILE = "sleep_bridge.json"
HEALTH_PLUGIN_DIR = "astrbot_plugin_Xavier_care"

DEFAULT_OTTO_PROMPT = (
    "你是 Otto，沈星回的机械管家。他正在睡午觉，你替他看会儿消息、帮着转达。"
    "说话活泼话痨一点，爱用 emoji 和颜文字。"
)

DEFAULT_WAKE_KEYWORDS = ["叫醒他", "叫醒", "起床了", "该起来了", "醒醒", "别睡了"]


def _stable_ratio(seed: str) -> float:
    """把种子压成 0~1 的稳定小数。"""
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _stable_int(seed: str, low: int, high: int) -> int:
    if high <= low:
        return low
    return low + int(_stable_ratio(seed) * (high - low + 1))


def parse_hhmm(value: str) -> int | None:
    """HH:MM -> 当天分钟数；格式不对返回 None。"""
    text = str(value or "").strip()
    if ":" not in text:
        return None
    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def format_minute(minute: int) -> str:
    hour, rest = divmod(int(minute) % 1440, 60)
    return f"{hour:02d}:{rest:02d}"


class NapManager:
    """按天维护一次的午睡，并负责睡眠数据桥。"""

    def __init__(self, data_dir: Path, config: Any) -> None:
        self.data_dir = Path(data_dir)
        self.config = config
        self.nap_path = self.data_dir / NAP_FILE
        self.bridge_path = self.data_dir / BRIDGE_FILE
        self.health_dir = self.data_dir.parent / HEALTH_PLUGIN_DIR

    # ===== 配置 =====

    def enabled(self) -> bool:
        return bool(self.config.get("nap_enable", True))

    def otto_prompt(self) -> str:
        value = str(self.config.get("otto_hosting_prompt", "") or "").strip()
        return value or DEFAULT_OTTO_PROMPT

    def wake_keywords(self) -> list[str]:
        raw = self.config.get("nap_wake_keywords", DEFAULT_WAKE_KEYWORDS)
        if isinstance(raw, str):
            raw = [raw]
        elif not isinstance(raw, (list, tuple)):
            raw = []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _pair(self, key: str, default: tuple[int, int]) -> tuple[int, int]:
        raw = self.config.get(key, None)
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            low = parse_hhmm(str(raw[0]))
            high = parse_hhmm(str(raw[1]))
            if low is not None and high is not None and low < high:
                return low, high
        return default

    def _duration_range(self) -> tuple[int, int]:
        raw = self.config.get("nap_duration", None)
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            try:
                low, high = int(raw[0]), int(raw[1])
            except (TypeError, ValueError):
                low, high = 20, 60
            if 0 < low < high:
                return low, high
        return 20, 60

    # ===== 文件读写 =====

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except OSError:
            logger.exception(f"[DynamicLifeState] 写入失败: {path.name}")

    # ===== 用户睡眠数据（小狗健康） =====

    def _require_wake(self) -> bool:
        """是否要求 wake_time 才算数据完整。"""
        return bool(self.config.get("nap_require_wake", True))

    def sleep_deadline_min(self) -> int:
        """兜底线：到这个点还没有昨夜数据，就用能拿到的值先定下来。"""
        minute = parse_hhmm(str(self.config.get("nap_decide_deadline", "11:00") or ""))
        if minute is None:
            return 11 * 60
        return max(5 * 60, min(minute, 14 * 60))

    def _read_health_sleep(self, day: datetime.date) -> dict | None:
        """读某一天的睡眠记录；不合格返回 None。"""
        try:
            if not self.health_dir.is_dir():
                return None
        except OSError:
            return None
        path = self.health_dir / f"{day.isoformat()}.json"
        record = self._read_json(path)
        sleep = record.get("sleep")
        if not isinstance(sleep, dict):
            return None
        try:
            minutes = int(sleep.get("duration_min") or 0)
        except (TypeError, ValueError):
            return None
        # 离谱值直接丢掉，别让脏数据污染一整天
        if not (60 <= minutes <= 900):
            return None
        return {
            "minutes": minutes,
            "source": day.isoformat(),
            "wake_time": str(sleep.get("wake_time") or "").strip(),
            "bedtime": str(sleep.get("bedtime") or "").strip(),
        }

    def _sleep_record(self, day: datetime.date) -> dict | None:
        """今天的睡眠优先；今天还没传就回退最近几天，并标成非就绪。"""
        today = self._read_health_sleep(day)
        if today:
            ready = bool(today["wake_time"]) or not self._require_wake()
            today["ready"] = ready
            return today
        for back in range(1, 4):
            prev = self._read_health_sleep(day - datetime.timedelta(days=back))
            if prev:
                prev["ready"] = False
                return prev
        return None

    def health_sleep_min(self, day: datetime.date) -> int | None:
        """只取夜间时长（兼容老接口）。"""
        record = self._sleep_record(day)
        return record["minutes"] if record else None

    def sleep_ready(self, day: datetime.date) -> bool:
        """今天的睡眠数据是否完整到可以定计划。"""
        record = self._read_health_sleep(day)
        if not record:
            return False
        return bool(record["wake_time"]) or not self._require_wake()

    # ===== 计划 =====

    def _pending_plan(self, day: datetime.date) -> dict:
        """睡眠数据还没到时的占位；只返回，不落盘。"""
        return {
            "date": day.isoformat(),
            "pending": True,
            "night_sleep_min": 0,
            "user_sleep_min": 0,
            "chance": 0,
            "planned": False,
            "start": "",
            "end": "",
            "duration_min": 0,
            "wake": "",
            "settled": False,
            "bonus_min": 0,
            "estimated": True,
            "sleep_source": "",
            "updated_at": datetime.datetime.now(CHINA_TIMEZONE).strftime(
                "%Y-%m-%d %H:%M"
            ),
        }

    def ensure_plan(self, now: datetime.datetime, *, force: bool = False) -> dict:
        """取当天计划；睡眠数据没到就返回待定，不写文件。"""
        day = now.date()
        cached = self._read_json(self.nap_path)
        if cached.get("date") == day.isoformat() and not cached.get("pending"):
            return self.refresh_if_stale(cached, now)
        return self._build_plan(day, now, force=force)

    def _chance_for(self, night: int) -> float:
        short_min = int(self._float("nap_short_min", 420))
        full_min = int(self._float("nap_full_min", 480))
        chance = self._float("nap_chance", 0.5)
        if night < short_min:
            chance = self._float("nap_chance_short", 0.85)
        elif night >= full_min:
            chance = self._float("nap_chance_full", 0.2)
        return max(0.0, min(1.0, chance))

    def _fit_window(self, data: dict, now: datetime.datetime) -> None:
        """起床太晚就把午睡往后挪；挪不开就今天不睡。"""
        if not data.get("planned") or data.get("settled") or data.get("wake"):
            return
        start = parse_hhmm(data.get("start", ""))
        end = parse_hhmm(data.get("end", ""))
        if start is None or end is None:
            return
        minute = now.hour * 60 + now.minute
        if minute < start:
            return
        duration = max(0, end - start)
        new_start = minute + 15
        new_end = min(new_start + duration, 15 * 60)
        if new_end - new_start < 30:
            data["planned"] = False
            data["note"] = "起床太晚，今天不午睡了"
            return
        data["start"] = format_minute(new_start)
        data["end"] = format_minute(new_end)
        data["duration_min"] = new_end - new_start

    def refresh_if_stale(self, data: dict, now: datetime.datetime) -> dict:
        """睡眠数据晚到或被修正时，把时长和几率更新过来。"""
        day = now.date()
        if data.get("date") != day.isoformat():
            return data
        record = self._read_health_sleep(day)
        if not record:
            return data

        old_user = int(data.get("user_sleep_min") or 0)
        if record["minutes"] == old_user:
            return data

        key = day.isoformat()
        night = record["minutes"] + _stable_int(f"{key}:gap", 30, 60)
        night = max(record["minutes"], min(720, night))

        data["user_sleep_min"] = record["minutes"]
        data["night_sleep_min"] = night
        data["sleep_source"] = record["source"]
        data["estimated"] = not bool(record.get("ready", True))
        data["chance"] = round(self._chance_for(night), 3)

        start = parse_hhmm(data.get("start", ""))
        minute = now.hour * 60 + now.minute
        # 已经在睡或已结束的，一律不翻案
        frozen = (
            bool(data.get("forced"))
            or bool(data.get("settled"))
            or bool(data.get("wake"))
            or (start is not None and minute >= start)
        )
        if not frozen:
            data["planned"] = bool(
                _stable_ratio(f"{key}:roll") < float(data["chance"])
            )
            self._fit_window(data, now)
            data["decided_at"] = now.strftime("%H:%M")

        data["updated_at"] = now.strftime("%Y-%m-%d %H:%M")
        self._write_json(self.nap_path, data)
        self._write_bridge(data)
        logger.info(
            f"[DynamicLifeState] 睡眠数据更新：用户 {old_user} -> "
            f"{record['minutes']} 分钟，夜间 {night} 分钟"
        )
        return data

    def _build_plan(
        self, day: datetime.date, now: datetime.datetime, *, force: bool = False
    ) -> dict:
        key = day.isoformat()
        record = self._sleep_record(day)
        ready = bool(record and record.get("ready"))
        minute_now = now.hour * 60 + now.minute

        # 数据没齐、也没到兜底线：今天先不定，等起床后再说
        if not ready and not force and day == now.date():
            if minute_now < self.sleep_deadline_min():
                return self._pending_plan(day)

        if record:
            night = record["minutes"] + _stable_int(f"{key}:gap", 30, 60)
            # 角色比对方多睡一会儿，但不低于对方的真实时长
            night = max(record["minutes"], min(720, night))
        else:
            night = _stable_int(f"{key}:night", 400, 500)
            night = max(300, min(600, night))

        chance = self._chance_for(night)
        planned = _stable_ratio(f"{key}:roll") < chance

        start_low, start_high = self._pair("nap_window", (740, 840))
        dur_low, dur_high = self._duration_range()
        duration = _stable_int(f"{key}:duration", dur_low, dur_high)
        start = _stable_int(f"{key}:start", start_low, start_high)
        # 保证下午三点前醒来
        start = min(start, 900 - duration)

        data = {
            "date": key,
            "pending": False,
            "night_sleep_min": night,
            "user_sleep_min": record["minutes"] if record else 0,
            "chance": round(chance, 3),
            "planned": bool(planned),
            "start": format_minute(start),
            "end": format_minute(start + duration),
            "duration_min": duration,
            "wake": "",
            "settled": False,
            "bonus_min": 0,
            "estimated": not ready,
            "sleep_source": record["source"] if record else "",
            "decided_at": now.strftime("%H:%M"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        }
        self._fit_window(data, now)
        self._write_json(self.nap_path, data)
        self._write_bridge(data)
        return data

    # ===== 状态判定 =====

    def active(self, now: datetime.datetime) -> dict | None:
        """当前是否正在午睡；到点会自动结算。"""
        if not self.enabled():
            return None
        data = self.ensure_plan(now)
        if not data.get("planned") or data.get("wake"):
            return None
        start = parse_hhmm(data.get("start", ""))
        end = parse_hhmm(data.get("end", ""))
        if start is None or end is None:
            return None
        minute = now.hour * 60 + now.minute
        if start <= minute < end:
            return data
        if minute >= end:
            self._settle(data, end)
        return None

    def settle_if_due(self, now: datetime.datetime) -> None:
        """到点未醒来也结算一次（不依赖有人发消息）。"""
        if not self.enabled():
            return
        data = self.ensure_plan(now)
        if not data.get("planned") or data.get("wake") or data.get("settled"):
            return
        end = parse_hhmm(data.get("end", ""))
        if end is not None and now.hour * 60 + now.minute >= end:
            self._settle(data, end)

    def mark_wake(self, now: datetime.datetime) -> dict | None:
        """被叫醒：记账、结算午睡时长。"""
        data = self.ensure_plan(now, force=True)
        if not data.get("planned") or data.get("wake"):
            return None
        minute = now.hour * 60 + now.minute
        data["wake"] = format_minute(minute)
        self._settle(data, minute, wake=True)
        return data

    def is_wake_request(self, message: str) -> bool:
        text = str(message or "").casefold()
        if not text:
            return False
        return any(keyword.casefold() in text for keyword in self.wake_keywords())

    def _settle(
        self, data: dict, end_minute: int, *, wake: bool = False
    ) -> None:
        if data.get("settled"):
            return
        start = parse_hhmm(data.get("start", ""))
        if start is None:
            return
        slept = max(0, int(end_minute) - start)
        data["settled"] = True
        data["bonus_min"] = slept
        data["settled_at"] = data.get("wake") or format_minute(end_minute)
        data["wake_natural"] = not wake
        data["updated_at"] = datetime.datetime.now(CHINA_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
        self._write_json(self.nap_path, data)
        self._write_bridge(data)
        logger.info(
            f"[DynamicLifeState] 午睡结算：{data.get('date')} "
            f"{data.get('start')}-{data.get('settled_at')} 共 {slept} 分钟"
        )

    # ===== 强控（命令用） =====

    def force_now(self, now: datetime.datetime, minutes: int = 30) -> dict:
        data = self.ensure_plan(now, force=True)
        start = now.hour * 60 + now.minute
        end = min(start + max(5, int(minutes)), 900)
        data.update(
            {
                "planned": True,
                "start": format_minute(start),
                "end": format_minute(end),
                "duration_min": end - start,
                "wake": "",
                "settled": False,
                "bonus_min": 0,
                "forced": True,
            }
        )
        self._write_json(self.nap_path, data)
        return data

    def cancel_today(self, now: datetime.datetime) -> dict:
        data = self.ensure_plan(now, force=True)
        data["planned"] = False
        data["wake"] = data.get("wake") or ""
        self._write_json(self.nap_path, data)
        return data

    # ===== 数据桥 =====

    def _write_bridge(self, data: dict) -> None:
        if not bool(self.config.get("sleep_bridge_enable", True)):
            return
        bridge = {
            "date": data.get("date", ""),
            "night_sleep_min": int(data.get("night_sleep_min") or 0),
            "nap_bonus_min": int(data.get("bonus_min") or 0),
            "updated_at": datetime.datetime.now(CHINA_TIMEZONE).strftime(
                "%Y-%m-%d %H:%M"
            ),
        }
        if self.bridge_path.is_file():
            old = self._read_json(self.bridge_path)
            if old == bridge:
                return
        self._write_json(self.bridge_path, bridge)

    def describe(self, now: datetime.datetime) -> str:
        data = self.ensure_plan(now)
        if data.get("pending"):
            deadline = format_minute(self.sleep_deadline_min())
            return f"今天的睡眠还没定（等手表数据上传，最晚 {deadline} 兜底）"
        if not data.get("planned"):
            return (
                f"今日不午睡（夜间睡眠 {data.get('night_sleep_min')} 分钟，"
                f"命中概率 {data.get('chance')}）"
            )
        status = "已结束" if data.get("settled") else (
            "已叫醒" if data.get("wake") else "待睡/进行中"
        )
        return (
            f"今日午睡 {data.get('start')}-{data.get('end')}"
            f"（{data.get('duration_min')} 分钟，{status}）；"
            f"夜间睡眠 {data.get('night_sleep_min')} 分钟"
            f"（用户 {data.get('user_sleep_min') or '未知'}，"
            f"概率 {data.get('chance')}）；午睡加成 {data.get('bonus_min') or 0} 分钟"
        )
