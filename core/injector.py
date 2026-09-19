import datetime
import uuid

from .state import (
    OPTIONAL_TIMELINE_FIELDS,
    LifeState,
    SlotMatch,
    format_datetime,
    format_interval,
)

FAKE_TOOL_CALL_NAME = "get_current_life_state"

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
            f"不必刻意提起节日，也不用说破，把它当成今天的情绪底色就好。\n"
        )
    return f"今天是{name}，可以留意一下今天的节日氛围。\n"

FAKE_TOOL_CALL_ID_PREFIX = "fake_dynamic_life_state_"


def build_injection_text(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> str:
    """构建临时的 <life_state> 上下文块。"""
    slot_label = "无"
    schedule_label = "无"
    interval_label = "无"
    optional_labels: list[str] = []
    if current_match:
        slot_label = current_match.entry.time
        schedule_label = current_match.entry.schedule
        for key, spec in OPTIONAL_TIMELINE_FIELDS.items():
            value = current_match.entry.extra_fields.get(key)
            if value:
                optional_labels.append(f"当前{spec['label']}: {value}\n")
        if current_match.active_interval:
            interval_label = format_interval(current_match.active_interval)

    text = (
        f"<life_state>\n"
        f"这是你当前的生活状态，可供参考\n"
        f"话题无关时无需主动提及\n"
        f"日期: {state.date}\n"
        f"今日节日: {state.holiday or '无'}\n"
        f"{_build_holiday_hint(state.holiday)}"
        f"当前日期时间: {format_datetime(now)}\n"
        f"今日概况: {state.schedule_summary or '无'}\n"
        f"今日氛围: {state.style_summary or '无'}\n"
        f"当前时段: {slot_label}\n"
        f"当前时段范围: {interval_label}\n"
        f"当前安排: {schedule_label}\n"
        f"{''.join(optional_labels)}"
        f"</life_state>"
    )
    return text


def build_fake_tool_call(
    state: LifeState,
    current_match: SlotMatch | None,
    now: datetime.datetime,
) -> list[dict]:
    inject_text = build_injection_text(state, current_match, now)
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