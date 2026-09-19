import datetime
import json
import re
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

CHINA_TIMEZONE = zoneinfo.ZoneInfo("Asia/Shanghai")

OPTIONAL_TIMELINE_FIELDS: dict[str, dict[str, str]] = {
    "mood": {"label": "心情", "icon": "🙂"},
    "emoji": {"label": "氛围", "icon": "✨"},
    "location": {"label": "地点", "icon": "📍"},
    "note": {"label": "备注", "icon": "🗒️"},
}

# 心情 → emoji 映射表。键为 emoji，值为关键词元组。
MOOD_EMOJI_TABLE: dict[str, tuple[str, ...]] = {
    "😄": ("开心", "愉快", "高兴", "兴奋", "雀跃", "元气", "满足", "舒畅",
           "轻快", "愉悦", "畅快", "开怀", "明朗", "兴致勃勃", "神清气爽"),
    "😌": ("慵懒", "懒散", "惬意", "悠闲", "悠哉", "放松", "安宁", "平静",
           "闲适", "散漫", "放空", "沉静", "恬淡", "松弛", "舒坦", "悠然",
           "安然", "清净", "慵困", "舒缓", "无所事事", "闲散"),
    "😪": ("困", "睡", "睡意", "疲倦", "疲惫", "乏", "迷糊", "打盹", "昏沉",
           "没精神", "萎靡", "倦", "惺忪"),
    "🥰": ("温柔", "心动", "亲昵", "甜", "眷恋", "想念", "柔软", "温情",
           "暖意", "安心", "缱绻", "依恋", "缱绻"),
    "🥺": ("委屈", "撒娇", "可怜", "心疼", "心软", "鼻酸", "眼巴巴",
           "想被哄", "酸涩", "无措", "蔫"),
    "🤔": ("专注", "沉思", "出神", "认真", "投入", "若有所思", "沉浸",
           "入神", "专心", "埋头", "钻研", "心无旁骛"),
    "😔": ("低落", "难过", "失落", "孤单", "寂寞", "感伤", "忧郁", "emo",
           "消沉", "沮丧", "空落", "怅然", "闷闷", "提不起劲"),
    "😕": ("烦躁", "烦", "焦虑", "不安", "恼", "犹豫", "纠结", "心乱",
           "焦躁", "压抑", "憋闷", "坐立难安"),
    "😬": ("紧张", "忐忑", "拘谨", "局促", "心虚", "慌张"),
    "😠": ("生气", "恼怒", "不耐烦", "火大", "愠怒", "赌气"),
    "😐": ("无聊", "淡然", "无语", "平淡", "麻木", "发呆", "敷衍", "例行"),
    "🤩": ("期待", "憧憬", "跃跃欲试", "兴致", "好奇", "跃动"),
}

DEFAULT_MOOD_EMOJI = "🙂"

# 展开为 (关键词, emoji) 序列并让长关键词优先匹配，
# 避免「不耐烦」被「烦」、「若有所思」被「专注」之类的短词抢先命中。
_MOOD_KEYWORD_INDEX: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (keyword, emoji)
            for emoji, keywords in MOOD_EMOJI_TABLE.items()
            for keyword in keywords
        ),
        key=lambda pair: -len(pair[0]),
    )
)


def mood_emoji(value: str | None) -> str:
    """按心情文本匹配 emoji；为空或未命中时回退到默认图标。"""
    text = (value or "").strip().lower()
    if not text:
        return DEFAULT_MOOD_EMOJI
    for keyword, emoji in _MOOD_KEYWORD_INDEX:
        if keyword in text:
            return emoji
    return DEFAULT_MOOD_EMOJI


def get_enabled_fields(config) -> dict[str, dict[str, str]]:
    """按配置过滤启用的可选字段，保持注册表顺序。"""
    raw = None
    if config is not None:
        try:
            raw = config.get("optional_fields_enabled", ["mood"])
        except Exception:
            raw = ["mood"]
    if not isinstance(raw, (list, tuple)):
        raw = ["mood"]
    return {
        k: OPTIONAL_TIMELINE_FIELDS[k]
        for k in OPTIONAL_TIMELINE_FIELDS
        if k in raw
    }


@dataclass(slots=True)
class TimelineEntry:
    time: str
    schedule: str
    extra_fields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, str] = {}
        for key in OPTIONAL_TIMELINE_FIELDS:
            value = self.extra_fields.get(key)
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()
        self.extra_fields = normalized

    @classmethod
    def from_dict(cls, data: dict) -> "TimelineEntry | None":
        required_fields: dict[str, str] = {}
        for key in ("time", "schedule"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                return None
            required_fields[key] = value.strip()
        if not is_valid_time_slot(required_fields["time"]):
            return None
        return cls(
            time=required_fields["time"],
            schedule=required_fields["schedule"],
            extra_fields=data,
        )

    def to_dict(self) -> dict:
        data = {
            "time": self.time,
            "schedule": self.schedule,
        }
        for key in OPTIONAL_TIMELINE_FIELDS:
            if key in self.extra_fields:
                data[key] = self.extra_fields[key]
        return data


@dataclass(slots=True)
class LifeState:
    date: str
    holiday: str = "无"
    schedule_summary: str = ""
    style_summary: str = ""
    timeline: list[TimelineEntry] = field(default_factory=list)
    status: str = "ok"
    generated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "LifeState":
        date = data.get("date")
        if not isinstance(date, str):
            raise ValueError("date field is missing or is not a string")
        datetime.date.fromisoformat(date)

        timeline_raw = data.get("timeline", [])
        if not isinstance(timeline_raw, list):
            timeline_raw = []
        timeline = [
            entry
            for entry in (
                TimelineEntry.from_dict(item)
                for item in timeline_raw
                if isinstance(item, dict)
            )
            if entry is not None
        ]
        status = str(data.get("status", "ok"))
        if status == "ok" and not timeline:
            raise ValueError("status is ok but the timeline has no valid entries")
        return cls(
            date=date,
            holiday=(
                data["holiday"].strip()
                if isinstance(data.get("holiday"), str) and data["holiday"].strip()
                else "无"
            ),
            schedule_summary=str(data.get("schedule_summary", "")),
            style_summary=str(data.get("style_summary", "")),
            timeline=timeline,
            status=status,
            generated_at=str(data.get("generated_at", "")),
        )

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "holiday": self.holiday,
            "schedule_summary": self.schedule_summary,
            "style_summary": self.style_summary,
            "timeline": [entry.to_dict() for entry in self.timeline],
            "status": self.status,
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True, slots=True)
class TimeInterval:
    start: int
    end: int

    def contains(self, minute: int) -> bool:
        return self.start <= minute < self.end


@dataclass(frozen=True, slots=True)
class SlotMatch:
    entry: TimelineEntry
    intervals: tuple[TimeInterval, ...]
    active_interval: TimeInterval | None = None


def format_datetime(value: datetime.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def format_interval(interval: TimeInterval) -> str:
    start_hour, start_minute = divmod(interval.start, 60)
    end_hour, end_minute = divmod(interval.end, 60)
    return f"[{start_hour:02d}:{start_minute:02d}, {end_hour:02d}:{end_minute:02d})"


MAX_HISTORY_DAYS = 7
_HISTORY_FILE_RE = re.compile(r"life_state_(\d{4}\.\d{2}\.\d{2})\.json")


class DataManager:
    def __init__(self, json_path: Path):
        self._path = json_path
        self._history_dir = json_path.parent / "history"
        self._data: LifeState | None = None
        self._backup_legacy_storage()
        self.load()

    def get_current(self, date: str) -> LifeState | None:
        datetime.date.fromisoformat(date)
        if self._data is None or self._data.date != date:
            return None
        return self._data

    def get_by_date(self, date: str) -> LifeState | None:
        datetime.date.fromisoformat(date)
        if self._data is not None and self._data.date == date:
            return self._data
        return self._load_path(self._history_path(date))

    def archive_before_generation(self, target_date: str) -> None:
        datetime.date.fromisoformat(target_date)
        if self._data is None or self._data.date == target_date:
            self._prune_history()
            return

        history_path = self._history_path(self._data.date)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = history_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self._data.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(history_path)

        previous = self._data
        self._data = None
        try:
            self.save()
        except Exception:
            self._data = previous
            raise
        self._prune_history()

    def get_recent_history(self, before_date: str, limit: int) -> list[LifeState]:
        parsed_before = datetime.date.fromisoformat(before_date)
        if limit <= 0 or not self._history_dir.exists():
            return []

        history_files: list[tuple[datetime.date, Path]] = []
        for path in self._history_dir.iterdir():
            match = _HISTORY_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                file_date = datetime.datetime.strptime(
                    match.group(1), "%Y.%m.%d"
                ).date()
            except ValueError:
                continue
            if file_date < parsed_before:
                history_files.append((file_date, path))

        states: list[LifeState] = []
        for file_date, path in sorted(history_files, reverse=True):
            state = self._load_path(path)
            if (
                state is None
                or state.date != file_date.isoformat()
                or state.status != "ok"
            ):
                continue
            states.append(state)
            if len(states) >= limit:
                break
        return states

    def set(self, state: LifeState) -> None:
        datetime.date.fromisoformat(state.date)
        previous = self._data
        self._data = state
        try:
            self.save()
        except Exception:
            self._data = previous
            raise

        history_path = self._history_path(state.date)
        if history_path.exists():
            try:
                history_path.unlink()
            except OSError as exc:
                logger.warning(
                    f"[DynamicLifeState] Failed to remove same-date history "
                    f"{history_path}: {exc}"
                )
        self._prune_history()

    def load(self) -> None:
        self._data = self._load_path(self._path)

    def save(self) -> None:
        if self._data is None:
            if self._path.exists():
                self._path.unlink()
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._data.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    @staticmethod
    def _history_filename(date: datetime.date) -> str:
        return f"life_state_{date.strftime('%Y.%m.%d')}.json"

    def _history_path(self, date: str) -> Path:
        parsed = datetime.date.fromisoformat(date)
        return self._history_dir / self._history_filename(parsed)

    @staticmethod
    def _load_path(path: Path) -> LifeState | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            return LifeState.from_dict(raw)
        except Exception:
            return None

    def _backup_legacy_storage(self) -> None:
        needs_backup = self._path.exists() and self._load_path(self._path) is None
        if self._history_dir.exists():
            for path in self._history_dir.iterdir():
                if (
                    _HISTORY_FILE_RE.fullmatch(path.name)
                    and self._load_path(path) is None
                ):
                    needs_backup = True
                    break
        if not needs_backup:
            return

        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        backup_dir = self._path.parent / f"legacy_backup_{timestamp}"
        backup_dir.mkdir(parents=True)
        moved: list[tuple[Path, Path]] = []
        try:
            for source in (self._path, self._history_dir):
                if not source.exists():
                    continue
                destination = backup_dir / source.name
                source.replace(destination)
                moved.append((source, destination))
        except Exception as exc:
            rollback_error: Exception | None = None
            for source, destination in reversed(moved):
                try:
                    destination.replace(source)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
            if rollback_error is not None:
                logger.error(
                    f"[DynamicLifeState] Legacy storage backup failed and "
                    f"rollback was incomplete; recovery data remains at "
                    f"{backup_dir}; backup error: {exc}; "
                    f"rollback error: {rollback_error}"
                )
                raise OSError(
                    f"Legacy storage backup failed and rollback was incomplete; "
                    f"recovery data remains at {backup_dir}"
                ) from rollback_error
            try:
                backup_dir.rmdir()
            except OSError as cleanup_exc:
                logger.warning(
                    f"[DynamicLifeState] Legacy storage rollback restored the "
                    f"original data, but failed to remove backup directory "
                    f"{backup_dir}: {cleanup_exc}"
                )
            raise OSError(
                "Legacy storage backup failed; original data restored"
            ) from exc

        logger.warning(f"[DynamicLifeState] Legacy state storage moved to {backup_dir}")

    def _prune_history(self) -> None:
        if not self._history_dir.exists():
            return

        history_files: list[tuple[datetime.date, Path]] = []
        for path in self._history_dir.iterdir():
            match = _HISTORY_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                file_date = datetime.datetime.strptime(
                    match.group(1), "%Y.%m.%d"
                ).date()
            except ValueError:
                continue
            state = self._load_path(path)
            if state is None or state.date != file_date.isoformat():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning(
                        f"[DynamicLifeState] Failed to remove invalid history "
                        f"file {path}: {exc}"
                    )
                continue
            history_files.append((file_date, path))

        for _, path in sorted(history_files, reverse=True)[MAX_HISTORY_DAYS:]:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning(
                    f"[DynamicLifeState] Failed to remove expired history file "
                    f"{path}: {exc}"
                )


_NATURAL_SLOTS: dict[str, tuple[int, int]] = {
    "凌晨": (0, 6 * 60),
    "早上": (6 * 60, 9 * 60),
    "上午": (9 * 60, 12 * 60),
    "中午": (12 * 60, 14 * 60),
    "下午": (14 * 60, 18 * 60),
    "傍晚": (18 * 60, 20 * 60),
    "晚上": (20 * 60, 23 * 60),
    "深夜": (23 * 60, 24 * 60),
}

NATURAL_SLOT_NAMES: tuple[str, ...] = tuple(_NATURAL_SLOTS.keys())

_TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})")
_CLOCK_TIME_RE = re.compile(r"(\d{2}):(\d{2})")


def _parse_time_slot(time_str: str) -> tuple[int, int] | None:
    value = time_str.strip()
    match = _TIME_RANGE_RE.fullmatch(value)
    if match:
        start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
        if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59):
            return None
        if not (0 <= end_hour <= 24 and 0 <= end_minute <= 59):
            return None
        if end_hour == 24 and end_minute != 0:
            return None
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start == end:
            return None
        return start, end

    for keyword, interval in _NATURAL_SLOTS.items():
        if keyword in value:
            return interval
    return None


def _is_specific_time_range(time_str: str) -> bool:
    return _TIME_RANGE_RE.fullmatch(time_str.strip()) is not None


def is_valid_time_slot(time_str: str) -> bool:
    return _parse_time_slot(time_str) is not None


def parse_clock_time(value: str) -> int:
    match = _CLOCK_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("time must use HH:MM format")
    hour, minute = map(int, match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time must be a valid 24-hour clock value")
    return hour * 60 + minute


def minute_of_day(value: datetime.datetime) -> int:
    return value.hour * 60 + value.minute


def resolve_entry_intervals(entry: TimelineEntry) -> tuple[TimeInterval, ...]:
    parsed = _parse_time_slot(entry.time)
    if parsed is None:
        return ()
    start, end = parsed
    if end <= start:
        end = 24 * 60
    return (TimeInterval(start, end),)


def entry_start_minute(entry: TimelineEntry) -> int | None:
    """返回条目的起始分钟；无法解析时返回 None。"""
    parsed = _parse_time_slot(entry.time)
    if parsed is None:
        return None
    return parsed[0]


def split_timeline_at(
    timeline: list[TimelineEntry],
    minute: int,
) -> tuple[list[TimelineEntry], list[TimelineEntry]]:
    """按起始时间把时间线拆成（已开始, 未开始）两组，两组都保持原顺序。"""
    started: list[TimelineEntry] = []
    upcoming: list[TimelineEntry] = []
    for entry in timeline:
        start = entry_start_minute(entry)
        if start is not None and start <= minute:
            started.append(entry)
        else:
            upcoming.append(entry)
    return started, upcoming


def _natural_slot_name(minute: int) -> str | None:
    for name, (start, end) in _NATURAL_SLOTS.items():
        if start <= minute < end:
            return name
    return None


def select_current_slot(
    timeline: list[TimelineEntry],
    current_minute: int,
) -> SlotMatch | None:
    if not timeline or not 0 <= current_minute < 24 * 60:
        return None

    specific_entries: list[TimelineEntry] = []
    natural_entries: list[TimelineEntry] = []
    for entry in timeline:
        if _parse_time_slot(entry.time) is None:
            continue
        if _is_specific_time_range(entry.time):
            specific_entries.append(entry)
        else:
            natural_entries.append(entry)

    for entry in specific_entries:
        intervals = resolve_entry_intervals(entry)
        for interval in intervals:
            if interval.contains(current_minute):
                return SlotMatch(entry, intervals, interval)

    for entry in natural_entries:
        intervals = resolve_entry_intervals(entry)
        for interval in intervals:
            if interval.contains(current_minute):
                return SlotMatch(entry, intervals, interval)

    current_slot_name = _natural_slot_name(current_minute)
    if current_slot_name is None:
        return None

    interval = TimeInterval(*_NATURAL_SLOTS[current_slot_name])
    return SlotMatch(
        TimelineEntry(time=current_slot_name, schedule="空闲"),
        (interval,),
        interval,
    )


def find_slot_by_name(
    timeline: list[TimelineEntry],
    name: str,
) -> SlotMatch | None:
    if name not in _NATURAL_SLOTS:
        return None

    for entry in timeline:
        if entry.time == name:
            intervals = resolve_entry_intervals(entry)
            active = intervals[0] if intervals else None
            return SlotMatch(entry, intervals, active)

    interval = TimeInterval(*_NATURAL_SLOTS[name])
    return SlotMatch(
        TimelineEntry(name, "空闲"),
        (interval,),
        interval,
    )