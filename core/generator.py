import asyncio
import datetime
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

import holidays

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .state import (
    CHINA_TIMEZONE,
    MAX_HISTORY_DAYS,
    DataManager,
    LifeState,
    TimelineEntry,
    get_enabled_fields,
    split_timeline_at,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"
_LEGACY_PROMPT_PLACEHOLDERS = (
    "{business_date}",
    "{cycle_start}",
    "{cycle_end}",
    "{timezone}",
)

MAX_AUTO_RETRIES_PER_DAY = 3

# 追加到提示词末尾的软约束，用于增加每日差异
_EXTRA_RANDOMNESS_SUFFIX = (
    "\n\n【额外随机性要求】\n"
    "请避免与历史状态中的日程、场景、心情过分相似；\n"
    "即使角色的日常有固定节奏，也请在细节上做出变化：\n"
    "例如换一组活动、换一种心情、换一种时段侧重。\n"
    "（但同一天的动线要保持连贯：新增地点要符合现实通勤，不要为了求新而频繁换地点。）\n"
    "不要照搬历史状态的句式与结构，尽量让今天显得独一份。"
)


def _render_template(template: str, **kwargs: str) -> str:
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: kwargs.get(match.group(1), match.group(0)),
        template,
    )


def _load_default_prompt_template() -> str:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    template = schema.get("prompt_template", {}).get("default")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("_conf_schema.json 中缺少有效的 prompt_template.default")
    return template


def _format_minute(minute: int) -> str:
    hour, minute_of_hour = divmod(minute, 60)
    return f"{hour:02d}:{minute_of_hour:02d}"


def _merge_locked_timeline(
    base_timeline: list[TimelineEntry],
    generated_timeline: list[TimelineEntry],
    lock_minute: int,
    date: str,
) -> list[TimelineEntry]:
    """用已开始的历史时段覆盖重新生成结果中同一时间范围的部分。"""
    locked, previous_upcoming = split_timeline_at(base_timeline, lock_minute)
    if not locked:
        return generated_timeline

    _, generated_upcoming = split_timeline_at(generated_timeline, lock_minute)
    if not generated_upcoming:
        logger.warning(
            f"[DynamicLifeState] {date}: model produced no slot starting after "
            f"{_format_minute(lock_minute)}; keeping the previous upcoming slots"
        )
        generated_upcoming = previous_upcoming

    return locked + generated_upcoming


@dataclass(slots=True)
class GenerationResult:
    succeeded: bool
    state: LifeState | None
    error: str | None
    used_previous_state: bool = False
    auto_retries_exhausted: bool = False
    skipped: bool = False
    lock_minute: int | None = None


class Generator:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        data_mgr: DataManager,
    ) -> None:
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self._gen_lock = asyncio.Lock()
        self._auto_failures: dict[str, int] = {}

    @property
    def is_generating(self) -> bool:
        return self._gen_lock.locked()

    def _get_prompt_template(self) -> str:
        configured = self.config.get("prompt_template", "")
        if isinstance(configured, str) and configured.strip():
            if any(
                placeholder in configured for placeholder in _LEGACY_PROMPT_PLACEHOLDERS
            ):
                logger.warning(
                    "[DynamicLifeState] Legacy prompt placeholders detected; "
                    "using the current default prompt"
                )
                return _load_default_prompt_template()
            return configured
        logger.warning(
            "[DynamicLifeState] prompt_template is empty; using the schema default"
        )
        return _load_default_prompt_template()

    def _get_temperature(self) -> float:
        """读取生成温度，做范围裁剪与类型保护。"""
        try:
            t = float(self.config.get("llm_temperature", 1.0))
        except (TypeError, ValueError):
            t = 1.0
        return max(0.0, min(2.0, t))

    def _pick_day_mood(self) -> str:
        """按配置随机抽一条今日基调；池为空时返回「无」。"""
        if not self.config.get("day_mood_enable", True):
            return "无"
        pool = self.config.get("day_mood_pool") or []
        if not isinstance(pool, (list, tuple)):
            return "无"
        cleaned = [str(x).strip() for x in pool if str(x).strip()]
        if not cleaned:
            return "无"
        return random.choice(cleaned)

    def _format_history(self, states: list[LifeState]) -> str:
        if not states:
            return "无"
        enabled = get_enabled_fields(self.config)
        sections: list[str] = []
        for state in states:
            lines = [
                f"[{state.date}]",
                f"整体日程：{state.schedule_summary or '无'}",
                f"整体氛围：{state.style_summary or '无'}",
                "时间线：",
            ]
            for entry in state.timeline:
                lines.append(f"- {entry.time}：{entry.schedule or '无'}")
                for key, spec in enabled.items():
                    value = entry.extra_fields.get(key)
                    if value:
                        lines.append(f"  - {spec['label']}：{value}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    async def generate(
        self,
        target_date: datetime.date,
        force: bool = False,
        extra: str | None = None,
        lock_before_minute: int | None = None,
    ) -> GenerationResult:
        async with self._gen_lock:
            date = target_date.isoformat()
            base_state = self.data_mgr.get_current(date)
            if base_state is not None and base_state.status != "ok":
                base_state = None

            if not force:
                if base_state is not None:
                    return GenerationResult(
                        succeeded=True, state=base_state, error=None
                    )

                if self._auto_failures.get(date, 0) >= MAX_AUTO_RETRIES_PER_DAY:
                    logger.warning(
                        f"[DynamicLifeState] Automatic generation for {date} "
                        f"reached {MAX_AUTO_RETRIES_PER_DAY} failed attempts; "
                        "use /life new for a manual retry"
                    )
                    return GenerationResult(
                        succeeded=False,
                        state=None,
                        error="Automatic generation attempts exhausted",
                        auto_retries_exhausted=True,
                    )

            lock_minute: int | None = None
            if lock_before_minute is not None:
                lock_minute = max(0, min(int(lock_before_minute), 24 * 60 - 1))
                if base_state is not None:
                    started, upcoming = split_timeline_at(
                        base_state.timeline, lock_minute
                    )
                    if started and not upcoming:
                        logger.info(
                            f"[DynamicLifeState] {date}: every slot already "
                            f"started before {_format_minute(lock_minute)}; "
                            "regeneration skipped"
                        )
                        return GenerationResult(
                            succeeded=True,
                            state=base_state,
                            error=None,
                            skipped=True,
                            lock_minute=lock_minute,
                        )

            debug = bool(self.config.get("debug_mode", False))
            temperature = self._get_temperature()
            day_mood = self._pick_day_mood()
            current_time = (
                _format_minute(lock_minute)
                if lock_minute is not None
                else datetime.datetime.now(CHINA_TIMEZONE).strftime("%H:%M")
            )

            try:
                logger.info(
                    f"[DynamicLifeState] Generating life state for {date} "
                    f"in {CHINA_TIMEZONE.key}, temperature={temperature}, "
                    f"day_mood={day_mood!r}, "
                    f"current_time={current_time}, lock_before={lock_minute}"
                )

                self.data_mgr.archive_before_generation(date)
                try:
                    history_days = int(self.config.get("history_reference_days", 3))
                except (TypeError, ValueError):
                    history_days = 3
                history_days = max(0, min(history_days, MAX_HISTORY_DAYS))
                history_states = self.data_mgr.get_recent_history(date, history_days)
                history_text = self._format_history(history_states)
                extra_text = (extra or "").strip() or "无"
                holiday = "无"
                try:
                    holiday_calendar = holidays.country_holidays(
                        "CN",
                        years=target_date.year,
                        observed=False,
                        language="zh_CN",
                    )
                    holiday = (
                        str(holiday_calendar.get(target_date) or "无").strip() or "无"
                    )
                except Exception as e:
                    logger.warning(
                        f"[DynamicLifeState] Failed to resolve holiday: {e}"
                    )

            except Exception as e:
                logger.error(
                    f"[DynamicLifeState] Generation preparation failed: {e}"
                )
                return self._failure_result(date, e, count_auto_failure=not force)

            try:
                persona = await self._get_persona()
                enabled_fields = get_enabled_fields(self.config)
                template = self._get_prompt_template()

                # 额外随机性：在模板末尾追加软约束
                if self.config.get("extra_randomness", True):
                    template = template + _EXTRA_RANDOMNESS_SUFFIX

                prompt = _render_template(
                    template,
                    date=date,
                    day_mood=day_mood,
                    holiday=holiday,
                    persona=persona,
                    history_states=history_text,
                    extra_requirements=extra_text,
                    timeline_optional_fields="、".join(
                        f"{name}（{spec['label']}）"
                        for name, spec in enabled_fields.items()
                    ),
                    current_time=current_time,
                    locked_section=self._build_locked_section(
                        base_state, lock_minute, current_time
                    ),
                )

                if debug:
                    logger.info(f"[DynamicLifeState] Generation prompt:\n{prompt}")

                provider = await self._get_provider()
                if not provider:
                    raise RuntimeError("No LLM provider is available")

                sid = f"dynamic_life_state_gen_{date}"

                # 优先传 temperature；provider 若不接受就退回无参调用
                try:
                    resp = await provider.text_chat(
                        prompt, session_id=sid, temperature=temperature
                    )
                except TypeError:
                    logger.info(
                        "[DynamicLifeState] provider.text_chat 不接受 temperature "
                        "参数，回退为默认调用"
                    )
                    resp = await provider.text_chat(prompt, session_id=sid)

                text = self._extract_completion_text(resp)

                if debug:
                    logger.info(f"[DynamicLifeState] Raw model response:\n{text}")

                payload = self._extract_json(text)
                generated_at = datetime.datetime.now(CHINA_TIMEZONE).isoformat()
                state = self._validate_and_build(
                    payload, target_date, generated_at, holiday
                )

                if lock_minute is not None and base_state is not None:
                    state.timeline = _merge_locked_timeline(
                        base_state.timeline,
                        state.timeline,
                        lock_minute,
                        date,
                    )

            except Exception as e:
                logger.error(f"[DynamicLifeState] Generation failed for {date}: {e}")
                return self._failure_result(date, e, count_auto_failure=not force)

            try:
                self.data_mgr.set(state)
            except Exception as e:
                logger.error(
                    f"[DynamicLifeState] Failed to persist generated state: {e}"
                )
                return self._failure_result(date, e, count_auto_failure=not force)

            if debug:
                logger.info(
                    f"[DynamicLifeState] Parsed state:\n"
                    f"{json.dumps(state.to_dict(), ensure_ascii=False, indent=2)}"
                )

            logger.info(f"[DynamicLifeState] Life state generated for {date}")
            return GenerationResult(succeeded=True, state=state, error=None)

    @staticmethod
    def _build_locked_section(
        base_state: LifeState | None,
        lock_minute: int | None,
        current_time: str,
    ) -> str:
        if lock_minute is None or base_state is None:
            return "（本次为全天生成，请完整生成当天从早到晚的所有时段。）"

        locked, _ = split_timeline_at(base_state.timeline, lock_minute)
        if not locked:
            return (
                f"（{current_time} 之前没有已经发生的时段，"
                f"请从 {current_time} 之后开始生成，不要编造更早的时段。）"
            )

        lines = [
            f"{current_time} 之前的以下时段已经开始，属于已经发生的事实：",
            *(
                f"- {json.dumps(entry.to_dict(), ensure_ascii=False)}"
                for entry in locked
            ),
            "",
            "【强制】上面这些时段必须原样保留，禁止修改、删除、重排，也不要重复输出。",
            f"【强制】只输出 {current_time} 之后尚未开始的时段，"
            f"每一项的起始时间都必须晚于 {current_time}；"
            "这一条优先于「timeline 至少包含 3 个时间段」的要求，"
            "只写 1-3 个时段即可，不要为了凑数量而编造已经过去的时段。",
        ]
        return "\n".join(lines)

    def _failure_result(
        self,
        date: str,
        error: Exception,
        *,
        count_auto_failure: bool = False,
    ) -> GenerationResult:
        if count_auto_failure:
            self._auto_failures[date] = self._auto_failures.get(date, 0) + 1
        previous = self.data_mgr.get_current(date)
        if previous and previous.status == "ok":
            return GenerationResult(
                succeeded=False,
                state=previous,
                error=str(error),
                used_previous_state=True,
            )
        return GenerationResult(succeeded=False, state=None, error=str(error))

    @staticmethod
    def _extract_persona_prompt(persona: object) -> str:
        if isinstance(persona, dict):
            return persona.get("system_prompt") or persona.get("prompt", "")
        return getattr(persona, "system_prompt", None) or getattr(persona, "prompt", "")

    async def _get_persona(self) -> str:
        persona_id = str(self.config.get("persona_id", "")).strip()
        if persona_id:
            try:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    return self._extract_persona_prompt(persona)
            except Exception as e:
                logger.warning(f"[DynamicLifeState] Failed to load persona: {e}")

        try:
            p = await self.context.persona_manager.get_default_persona_v3()
            return self._extract_persona_prompt(p) if p else ""
        except Exception:
            return ""

    async def _get_provider(self):
        provider_id = str(self.config.get("llm_provider_id", "")).strip()
        if provider_id:
            p = self.context.get_provider_by_id(provider_id)
            if p is not None:
                return p
            logger.warning(
                f"[DynamicLifeState] llm_provider_id={provider_id} not found; "
                "falling back to current default provider"
            )
        return self.context.get_using_provider()

    @staticmethod
    def _extract_completion_text(resp: object) -> str:
        if resp is None:
            return ""
        for key in ("completion_text", "completion", "text", "content"):
            value = getattr(resp, key, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        text = text.strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

        start = text.find("{")
        if start == -1:
            return None

        brace = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                    if brace == 0:
                        try:
                            data = json.loads(text[start : i + 1])
                            return data if isinstance(data, dict) else None
                        except Exception:
                            return None
        return None

    @staticmethod
    def _validate_and_build(
        payload: dict | None,
        target_date: datetime.date,
        generated_at: str,
        holiday: str,
    ) -> LifeState:
        if not payload:
            raise ValueError("未能从模型输出中解析出 JSON 对象")

        date = target_date.isoformat()
        date_value = payload.get("date")
        if date_value != date:
            raise ValueError(
                f"date 字段必须与目标日期一致: expected={date}, actual={date_value}"
            )

        timeline_raw = payload.get("timeline")
        if not isinstance(timeline_raw, list) or len(timeline_raw) == 0:
            raise ValueError("timeline 字段缺失或为空列表")

        entries: list[TimelineEntry] = []
        for item in timeline_raw:
            if not isinstance(item, dict):
                continue
            entry = TimelineEntry.from_dict(item)
            if entry is not None:
                entries.append(entry)

        if not entries:
            raise ValueError("timeline 中没有有效条目")

        return LifeState(
            date=date,
            holiday=holiday,
            schedule_summary=str(payload.get("schedule_summary", "")),
            style_summary=str(payload.get("style_summary", "")),
            timeline=entries,
            status="ok",
            generated_at=generated_at,
        )