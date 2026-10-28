import datetime
import uuid

from .state import (
    LifeState,
    SlotMatch,
    format_clock,
    format_datetime,
    format_interval,
    period_for_minute,
    period_range,
    resolve_window,
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


def _build_rolling_text(
    state: LifeState,
    now: datetime.datetime,
    *,
    window: tuple[int, int] | None = None,
) -> str:
    """滚动模式：只展示已发生时段和当前已生成时段，不泄露未来安排。"""
    start, end = resolve_window(window)
    now_minute = now.hour * 60 + now.minute
    current_name = period_for_minute(now_minute, window)
    current_period = state.periods.get(current_name) if current_name else None

    display_name = current_name
    if current_period is None or current_period.status != "done":
        names = list(state.periods.keys())
        display_name = None
        if current_name in names:
            for prev in reversed(names[: names.index(current_name)]):
                if state.periods[prev].status == "done":
                    display_name = prev
                    break
        current_period = (
            state.periods.get(display_name) if display_name else None
        )

    past_lines: list[str] = []
    for name, period in state.periods.items():
        if name in (current_name, display_name):
            continue
        if period.status == "done" and period.summary:
            note = (
                f"（临时变动：{period.live_note}）"
                if period.live_note
                else ""
            )
            past_lines.append(
                f"- {name}：{period.summary}{note}"
                f"（{period.mood or '平静'}｜{period.location or '不明'}）\n"
            )

    current_lines: list[str] = []
    if current_period and current_period.status == "done":
        interval = _period_interval(display_name, window)
        current_lines = [
            f"最近一次已生成时段: {display_name}\n",
            (
                f"该时段范围: {format_interval(interval)}\n"
                if interval
                else ""
            ),
            f"最近安排: {current_period.summary}\n",
            f"当时心情: {current_period.mood or '平静'}\n",
            f"当时地点: {current_period.location or '不明'}\n",
        ]
        if current_period.carry_over:
            current_lines.append(
                "可自然延续的线索: "
                + "、".join(current_period.carry_over)
                + "\n"
            )
        if current_period.live_note:
            current_lines.append(
                f"临时变动: {current_period.live_note}\n"
            )
    elif current_name:
        current_lines = [
            f"当前时段: {current_name}\n",
            "这一时段的生活还没有生成。不要编造具体做了什么事；"
            "如果对方问起，只能用「还没想好」「待会儿再说」这类模糊说法。\n",
        ]
    else:
        current_lines = [
            "现在不在日程窗口内，当前没有已生成的时段。\n"
            "按平时的作息和聊天内容自然回应，不要主动交代几点睡、几点醒。\n"
        ]

    time_hint = ""
    if now_minute < start:
        time_hint = (
            f"现在是 {format_clock(start)} 之前，今天的日程还没开始。\n"
            "不要提前把今天的安排说成已经发生的事。\n"
        )
    elif now_minute >= end:
        time_hint = (
            f"现在已经是 {format_clock(end)} 之后，今天的日程结束了。\n"
            "后面的时间没有写进状态里，按平时作息自然推进就好。\n"
        )

    return (
        f"<life_state>\n"
        f"这是你当前的生活状态，可供参考\n"
        f"话题无关时无需主动提及\n"
        f"日期: {state.date}\n"
        f"今日节日: {state.holiday or '无'}\n"
        f"{_build_holiday_hint(state.holiday)}"
        f"当前日期时间: {format_datetime(now)}\n"
        f"—— 已经过去的时段（已经发生，是事实）——\n"
        f"{''.join(past_lines) or '（暂无）\n'}"
        f"—— 现在 ——\n"
        f"{''.join(current_lines)}"
        f"【提醒】不要提到还没生成的未来时段，也不要把计划说成已经做过的事。\n"
        f"{time_hint}"
        f"</life_state>"
    )


def _period_interval(
    name: str, window: tuple[int, int] | None
):
    value = period_range(name, window)
    if not value:
        return None
    return _SimpleInterval(value[0], value[1])


class _SimpleInterval:
    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end


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
    # 只走滚动口径，任何情况下都不把未来时段塞给模型。
    return _build_rolling_text(state, now, window=window)


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
