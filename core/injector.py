import datetime
import uuid

from .state import (
    OPTIONAL_TIMELINE_FIELDS,
    LifeState,
    SlotMatch,
    clamp_day_start,
    day_offset,
    format_clock,
    format_datetime,
    format_interval,
    resolve_window,
    split_timeline_at,
)

FAKE_TOOL_CALL_NAME = "get_current_life_state"

FAKE_TOOL_CALL_ID_PREFIX = "fake_dynamic_life_state_"

_REUNION_HOLIDAY_KEYWORDS = (
    "中秋", "除夕", "春节", "元宵", "七夕", "情人节", "圣诞", "元旦", "跨年", "冬至",
)


def _build_holiday_hint(holiday: str) -> str:
    """按节日性质生成一句轻提示；无节日时返回空串。"""
    name = (holiday or "").strip()
    if not name or name == "无":
        return ""
    if any(k in name for k in _REUNION_HOLIDAY_KEYWORDS):
        return (
            f"今天是{name}，团圆的日子。可以让想念自然浮上来，"
            "不必刻意提起节日，也不用说破，把它当成今天的情绪底色就好。\n"
        )
    return f"今天是{name}，可以留意一下今天的节日氛围。\n"


def build_injection_text(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
    *,
    window: tuple[int, int] | None = None,
    day_start: int | None = None,
) -> str:
    """构建临时的 <life_state> 上下文块。

    window 为日程窗口 (起点, 终点)：窗口之外的时间不属于日程范围，
    只给一句轻提示，具体状态交给模型结合上下文判断。
    day_start 为生活日起点（分钟），用于把时间线拆成「已发生 / 还没到」两半。
    """
    slot_label = "无"
    schedule_label = "无"
    interval_label = "无"
    optional_labels: list[str] = []
    if current_match:
        slot_label = current_match.entry.time
        schedule_label = current_match.entry.schedule or "空闲"
        for key, spec in OPTIONAL_TIMELINE_FIELDS.items():
            value = current_match.entry.extra_fields.get(key)
            if value:
                optional_labels.append(f"当前{spec['label']}: {value}\n")
        if current_match.active_interval:
            interval_label = format_interval(current_match.active_interval)

    start, end = resolve_window(window)
    now_minute = now.hour * 60 + now.minute

    # 按生活日偏移把时间线拆成两组：已经发生的、还没到的，
    # 避免把未来安排当成已经发生的事实交给模型。
    day_start_minute = clamp_day_start(day_start)
    current_entry = current_match.entry if current_match else None
    started, upcoming = split_timeline_at(
        state.timeline, day_offset(now_minute, day_start_minute), day_start_minute
    )
    happened_lines = [
        f"- {entry.time}：{entry.schedule or '空闲'}\n"
        for entry in started
        if entry is not current_entry
    ]
    upcoming_lines = [
        f"- {entry.time}：{entry.schedule or '空闲'}（还没发生）\n"
        for entry in upcoming
    ]

    time_hint = ""
    if start <= end:
        if now_minute < start:
            time_hint = (
                f"现在是 {format_clock(start)} 之前，今天的日程还没开始：\n"
                "你有可能还醒着，也可能已经睡下，"
                "不要仅因为时间晚就认定自己困了或该睡了。\n"
                "按平时的作息和对方说的话自然回应，不要主动交代几点睡、几点醒。\n"
            )
        elif now_minute >= end:
            time_hint = (
                f"现在已经是 {format_clock(end)} 之后，今天的日程到这里就结束了：\n"
                "后面的时间没有写进状态里，按平时的作息和聊天内容自然推进就好；\n"
                "你有可能醒着、也可能已经睡下，不要仅因为时间晚就说困或催对方休息。\n"
            )

    text = (
        f"<life_state>\n"
        f"这是你当前的生活状态，可供参考\n"
        f"话题无关时无需主动提及\n"
        f"日期: {state.date}\n"
        f"今日节日: {state.holiday or '无'}\n"
        f"{_build_holiday_hint(state.holiday)}"
        f"当前日期时间: {format_datetime(now)}\n"
        f"今日基调（整天的走向，不是已经发生的事）: "
        f"{state.schedule_summary or '无'}\n"
        f"今日氛围（整体感觉，不是已经发生的事）: "
        f"{state.style_summary or '无'}\n"
        f"—— 已经过去的时段（已经发生，是事实）——\n"
        f"{''.join(happened_lines) or '（暂无）\n'}"
        f"—— 现在 ——\n"
        f"当前时段: {slot_label}\n"
        f"当前时段范围: {interval_label}\n"
        f"当前安排: {schedule_label}\n"
        f"{''.join(optional_labels)}"
        f"—— 还没到的时段（只是计划，还没有发生）——\n"
        f"{''.join(upcoming_lines) or '（暂无）\n'}"
        f"【提醒】上面「还没到」的时段只是计划，别把它们说成已经做过的事，"
        f"也不要提前把后面的安排消耗掉；对方问起时只能说「打算」「应该会」。\n"
        f"{time_hint}"
        f"</life_state>"
    )
    return text


def build_fake_tool_call(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
    *,
    window: tuple[int, int] | None = None,
    day_start: int | None = None,
) -> list[dict]:
    inject_text = build_injection_text(
        state, current_match, now, window=window, day_start=day_start
    )
    call_id = f"{FAKE_TOOL_CALL_ID_PREFIX}{uuid.uuid4().hex[:12]}"

    assistant_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": FAKE_TOOL_CALL_NAME,
                    "arguments": "{}",
                },
            }
        ],
    }

    tool_msg = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": FAKE_TOOL_CALL_NAME,
        "content": inject_text,
    }

    return [assistant_msg, tool_msg]


def remove_fake_tool_call_from_context(contexts: list[dict]) -> None:
    if not contexts:
        return

    indices_to_remove: set[int] = set()
    fake_call_ids: set[str] = set()

    for i, msg in enumerate(contexts):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                if tc_id.startswith(FAKE_TOOL_CALL_ID_PREFIX):
                    fake_call_ids.add(tc_id)
                    indices_to_remove.add(i)
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id in fake_call_ids:
                indices_to_remove.add(i)

    for i in sorted(indices_to_remove, reverse=True):
        contexts.pop(i)
