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
    day_offset,
    day_start_from_config,
    entries_in_window,
    entry_start_minute,
    format_clock,
    get_enabled_fields,
    natural_slots,
    split_timeline_at,
    timeline_window_from_config,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"
_LEGACY_PROMPT_PLACEHOLDERS = (
    "{business_date}",
    "{cycle_start}",
    "{cycle_end}",
    "{timezone}",
)

MAX_AUTO_RETRIES_PER_DAY = 3
SLOT_ORDER = ("上午", "中午", "下午", "晚上")
DEFAULT_SLACK_RATIO = 0.3

# 追加到提示词末尾的软约束，用于增加每日差异
_EXTRA_RANDOMNESS_SUFFIX = (
    "\n\n【关于和前几天相似】\n"
    "不要照搬历史状态的句式与结构，换个说法即可；\n"
    "但相似本身是允许的：如果角色今天本来就过得和前几天差不多，就照实写，\n"
    "不要为了显得新鲜而硬改地点、活动或心情。\n"
    "（同一天的动线要保持连贯：真的需要移动时才移动，且要符合现实通勤。）"
)


def _render_template(template: str, **kwargs: str) -> str:
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: kwargs.get(match.group(1), match.group(0)),
        template,
    )


def _load_default_prompt_template() -> str:
    try:
        # 兼容带 UTF-8 BOM 的配置文件（Windows 记事本编辑后常见）
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"_conf_schema.json 读取失败：{e}") from e
    template = schema.get("prompt_template", {}).get("default")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("_conf_schema.json 中缺少有效的 prompt_template.default")
    return template


def _merge_locked_timeline(
    base_timeline: list[TimelineEntry],
    generated_timeline: list[TimelineEntry],
    lock_offset: int,
    day_start: int,
    window: tuple[int, int],
    date: str,
) -> list[TimelineEntry]:
    """用已开始的历史时段覆盖重新生成结果中同一时间范围的部分。"""
    locked, previous_upcoming = split_timeline_at(
        base_timeline, lock_offset, day_start
    )
    if not locked:
        return generated_timeline

    _, generated_upcoming = split_timeline_at(
        generated_timeline, lock_offset, day_start
    )
    if not generated_upcoming:
        logger.warning(
            f"[DynamicLifeState] {date}: model produced no slot starting after "
            f"offset {lock_offset}; keeping the previous upcoming slots"
        )
        generated_upcoming = previous_upcoming

    locked = entries_in_window(locked, window)
    generated_upcoming = entries_in_window(generated_upcoming, window)
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

    def _day_start(self) -> int:
        """读取生活日起点（分钟）。"""
        return day_start_from_config(self.config.get("day_start", "06:00"))

    def _window(self) -> tuple[int, int]:
        """读取日程窗口（起点分钟, 终点分钟）。"""
        return timeline_window_from_config(self.config)

    def _build_day_rule_section(
        self, target_date: datetime.date, window: tuple[int, int]
    ) -> str:
        """把日程范围、时段划分、写作风格写成最终口径，追加到提示词末尾。"""
        start_text = format_clock(window[0])
        end_text = format_clock(window[1])
        return (
            "\n\n【系统约束·最终口径（若与上文冲突，一律以本段为准）】\n"
            f"1. 这一天只写 {start_text} 到 {end_text} 之间的事："
            f"时间线从 {start_text} 写起（起床、通勤这些 {start_text} 之前的内容不写），"
            f"到 {end_text} 收尾。\n"
            f"2. {end_text} 之后到次日 {start_text} 属于本状态的空白区："
            "不要出现在时间线里，也不要交代「几点睡、几点醒」这类作息细节；"
            f"最后一段写到 {end_text} 前后（例如「收拾东西准备休息」）即可收笔。\n"
            "3. 时间线只使用这 4 个时段，顺序固定：\n"
            f"   上午 {start_text}-12:00 ｜ 中午 12:00-14:00 ｜ 下午 14:00-18:00 ｜ "
            f"晚上 18:00-{end_text}\n"
            "4. 这 4 个时段都要写出来，且只能出现这 4 个名字，"
            "不要自创「深夜 / 凌晨 / 早上 / 傍晚」之类的时段；"
            "某一段确实没什么安排，就写「空闲」。\n"
            "5. schedule 写清楚这个时段在做什么，长度适中（约 10~30 字）："
            "写具体的事，可以带上地点或对象，但不要写成大段场景描写或抒情。\n"
            "   schedule_summary / style_summary 写成一两句白话概括即可。\n"
            "6. 全天动线要连贯：地点与状态顺着这 4 个时段自然推进，"
            "允许相邻时段待在同一地点、也允许某个时段只是延续上一段的状态；"
            "不要为了求新频繁换地点，也不要在「晚上」之后继续往后铺陈。\n"
            "7. 可以接着昨天的状态往下过：如果昨天在做的是一件长期的事"
            "（连载、习惯、正在追的东西、正在处理的事情），今天自然续上就好，"
            "不必每天重新开一个头，也不必硬凑出新的安排。\n"
        )


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

    def _get_slack_ratio(self) -> float:
        """读取摆烂概率，异常或越界时回退默认值。"""
        try:
            ratio = float(self.config.get("slack_ratio", DEFAULT_SLACK_RATIO))
        except (TypeError, ValueError):
            ratio = DEFAULT_SLACK_RATIO
        return max(0.0, min(1.0, ratio))

    def _pick_slack_slots(
        self, target_date: datetime.date, window: tuple[int, int]
    ) -> set[str]:
        """按日期稳定掷骰，返回当天命中的摆烂时段。"""
        ratio = self._get_slack_ratio()
        if ratio <= 0:
            return set()
        slots = [name for name in SLOT_ORDER if name in natural_slots(window)]
        if ratio >= 1:
            return set(slots)
        rng = random.Random(f"{target_date.isoformat()}:slack")
        return {name for name in slots if rng.random() < ratio}

    @staticmethod
    def _slots_for_entries(
        entries: list[TimelineEntry], window: tuple[int, int]
    ) -> set[str]:
        """把时间线条目映射回 4 个粗时段名。"""
        ranges = natural_slots(window)
        names: set[str] = set()
        for entry in entries:
            minute = entry_start_minute(entry, window)
            if minute is None:
                continue
            for name, slot_ranges in ranges.items():
                if any(start <= minute < end for start, end in slot_ranges):
                    names.add(name)
                    break
        return names

    @staticmethod
    def _build_slack_section(slack_slots: set[str]) -> str:
        """把命中的摆烂时段写成提示词约束；无命中时整段不追加。"""
        if not slack_slots:
            return ""
        ordered = [name for name in SLOT_ORDER if name in slack_slots]
        slot_text = "、".join(ordered)
        sep = chr(10)
        lines = [
            "",
            "",
            "【今日摆烂时段】",
            f"今天以下时段允许且应当摆烂：{slot_text}。",
            "1. 这些时段只做最低限度的事：赖床、窝着、发呆、刷手机、"
            "懒得收拾、不出门、不约人、不安排正经事。",
            "2. 仍然要写具体（约 10~30 字），不要只写「摆烂」两个字。",
            "3. 不写吃了什么（沿用饮食禁令）。",
            "4. 摆烂不等于丧：语气松弛、懒散，不写成低落、抱怨或自我否定。",
            "5. 未命中的时段照常写，不要被带得一起瘫。",
            "6. 允许 4 段全命中，那就是完整摆烂的一天。",
            "7. 本段优先级高于「每天要和平时不一样」的软约束。",
            "8. 地点锁定：摆烂时段的活动地点尽量在室内或角色常居处"
            "（家、住处、常去的室内角落），不要人在摆烂、地点却满城乱跑；"
            "非必要不新增外出动线。",
            "9. 心情口径偏慵懒、放空、松弛，可以写「懒得动」「脑子空空的」"
            "「就这么窝着」，不写成焦虑、低落、自我否定。",
            "10. 与最终口径的动线规则一致：相邻时段若都摆烂，"
            "地点自然承接，不为了换场景而换场景。",
            "",
        ]
        return sep.join(lines)

    def _format_history(self, states: list[LifeState]) -> str:
        if not states:
            return "无"
        enabled = get_enabled_fields(self.config)
        window = self._window()
        sections: list[str] = []
        for state in states:
            lines = [
                f"[{state.date}]",
                f"整体日程：{state.schedule_summary or '无'}",
                f"整体氛围：{state.style_summary or '无'}",
                "时间线：",
            ]
            for entry in entries_in_window(state.timeline, window):
                lines.append(f"- {entry.time}：{entry.schedule or '无'}")
                for key, spec in enabled.items():
                    value = entry.extra_fields.get(key)
                    if value:
                        lines.append(f"  - {spec['label']}：{value}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections) + (
            "\n\n（上面是最近几天的情况。可以让作息、习惯和正在做的事"
            "自然地延续下去，不必每天换一套过法；写起来像同一个人连着过的日子即可。）"
        )

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

            day_start = self._day_start()
            window = self._window()
            lock_minute: int | None = None
            lock_offset: int | None = None
            if lock_before_minute is not None:
                lock_minute = max(0, min(int(lock_before_minute), 24 * 60 - 1))
                lock_offset = day_offset(lock_minute, day_start)
                if base_state is not None:
                    started, upcoming = split_timeline_at(
                        base_state.timeline, lock_offset, day_start
                    )
                    if started and not upcoming:
                        logger.info(
                            f"[DynamicLifeState] {date}: every slot already "
                            f"started before {format_clock(lock_minute)}; "
                            "regeneration skipped"
                        )
                        return GenerationResult(
                            succeeded=True,
                            state=base_state,
                            error=None,
                            skipped=True,
                            lock_minute=lock_minute,
                        )

            slack_slots: set[str] = set()
            if lock_offset is not None and base_state is not None:
                started, _ = split_timeline_at(
                    base_state.timeline, lock_offset, day_start
                )
                locked_slots = self._slots_for_entries(
                    entries_in_window(started, window), window
                )
                slack_slots = (
                    self._pick_slack_slots(target_date, window) - locked_slots
                )
            else:
                slack_slots = self._pick_slack_slots(target_date, window)

            debug = bool(self.config.get("debug_mode", False))
            temperature = self._get_temperature()
            day_mood = self._pick_day_mood()
            current_time = (
                format_clock(lock_minute)
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
                extra_blocks: list[str] = []
                if extra and extra.strip():
                    extra_blocks.append(extra.strip())
                extra_text = "\n".join(extra_blocks) or "无"
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

                # 日程窗口 / 时段划分 / 写作风格的最终口径
                template = template + self._build_day_rule_section(
                    target_date, window
                )

                # 今日摆烂时段（无命中时不追加任何内容）
                template = template + self._build_slack_section(slack_slots)
                if debug and slack_slots:
                    logger.info(
                        f"[DynamicLifeState] {date}: slack slots="
                        f"{'/'.join(n for n in SLOT_ORDER if n in slack_slots)}"
                    )

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
                    day_start=format_clock(day_start),
                    timeline_start=format_clock(window[0]),
                    timeline_end=format_clock(window[1]),
                    locked_section=self._build_locked_section(
                        base_state, lock_offset, current_time, day_start, window
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

                if lock_offset is not None and base_state is not None:
                    state.timeline = _merge_locked_timeline(
                        base_state.timeline,
                        state.timeline,
                        lock_offset,
                        day_start,
                        window,
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
        lock_offset: int | None,
        current_time: str,
        day_start: int,
        window: tuple[int, int],
    ) -> str:
        if lock_offset is None or base_state is None:
            return "（本次为整天生成，请按 4 个时段完整输出这一天。）"

        locked, _ = split_timeline_at(base_state.timeline, lock_offset, day_start)
        locked = entries_in_window(locked, window)
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
            "这一条优先于「4 个时段都要写出来」的要求，"
            "只写 1~3 个时段即可，不要为了凑数量而编造已经过去的时段。",
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

    @staticmethod
    def _persona_label(persona: object) -> str:
        if isinstance(persona, dict):
            return str(persona.get("name") or persona.get("id") or "未命名")
        return str(
            getattr(persona, "name", None)
            or getattr(persona, "id", None)
            or "未命名"
        )

    async def _get_persona(self) -> str:
        persona_id = str(self.config.get("persona_id", "")).strip()
        if persona_id:
            try:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    logger.debug(
                        f"[DynamicLifeState] 使用配置人格："
                        f"id={persona_id}, name={self._persona_label(persona)}"
                    )
                    return self._extract_persona_prompt(persona)
                logger.warning(
                    f"[DynamicLifeState] persona_id={persona_id} 不存在或读取为空，"
                    "已回退默认人格"
                )
            except Exception as e:
                logger.warning(
                    f"[DynamicLifeState] 读取人格 {persona_id} 失败，已回退默认人格：{e}"
                )
        else:
            logger.debug(
                "[DynamicLifeState] persona_id 未配置，使用默认人格生成生活状态"
            )

        try:
            p = await self.context.persona_manager.get_default_persona_v3()
            if p:
                logger.debug(
                    f"[DynamicLifeState] 使用默认人格：{self._persona_label(p)}"
                )
            return self._extract_persona_prompt(p) if p else ""
        except Exception as e:
            logger.warning(f"[DynamicLifeState] 读取默认人格失败：{e}")
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

    def _validate_and_build(
        self,
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

        window = self._window()
        filtered = entries_in_window(entries, window)
        if len(filtered) != len(entries):
            logger.info(
                f"[DynamicLifeState] {date}: dropped "
                f"{len(entries) - len(filtered)} timeline entries outside "
                f"{format_clock(window[0])}-{format_clock(window[1])}"
            )
        if not filtered:
            raise ValueError("timeline 中没有落在日程窗口内的条目")
        entries = filtered

        return LifeState(
            date=date,
            holiday=holiday,
            schedule_summary=str(payload.get("schedule_summary", "")),
            style_summary=str(payload.get("style_summary", "")),
            timeline=entries,
            status="ok",
            generated_at=generated_at,
        )