"""滚动生成生活状态：每次只生成一个粗时段，done 后不再改写。"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

import holidays

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .meal_link import MealLink
from .state import (
    CHINA_TIMEZONE,
    NATURAL_SLOT_NAMES,
    DataManager,
    LifeState,
    PeriodState,
    TimelineEntry,
    day_start_from_config,
    format_clock,
    minute_of_day,
    mood_emoji,
    period_for_entry,
    period_for_minute,
    period_names,
    period_range,
    timeline_window_from_config,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

MAX_AUTO_RETRIES_PER_DAY = 3
DEFAULT_PERIOD_MAX_RETRIES = 3
DEFAULT_GENERATION_TIMEOUT = 90
DEFAULT_CATCH_UP_LIMIT = 3
SLOT_ORDER = ("上午", "中午", "下午", "晚上")

_ALLOWED_LOCATIONS = (
    "家里",
    "公司",
    "学校",
    "路上",
    "店里",
    "公园",
    "河边",
    "外地",
    "不明",
)
_LOCATION_KEYWORDS = (
    ("家", "家里"),
    ("住处", "家里"),
    ("公寓", "家里"),
    ("公司", "公司"),
    ("办公室", "公司"),
    ("学校", "学校"),
    ("教室", "学校"),
    ("图书馆", "学校"),
    ("路", "路上"),
    ("地铁", "路上"),
    ("公交", "路上"),
    ("店", "店里"),
    ("超市", "店里"),
    ("商场", "店里"),
    ("公园", "公园"),
    ("河", "河边"),
    ("湖", "河边"),
    ("外地", "外地"),
    ("出差", "外地"),
    ("旅行", "外地"),
)

_EXTRA_RANDOMNESS_SUFFIX = (
    "\n\n【关于和前几天相似】\n"
    "不要照搬历史状态的句式与结构，换个说法即可；\n"
    "但相似本身是允许的：如果角色今天本来就过得和前几天差不多，就照实写，\n"
    "不要为了显得新鲜而硬改地点、活动或心情。"
)

_FALLBACK_PROMPT = """请根据角色设定，只生成角色在指定日期、指定时段的生活状态。

日期：{date}
当前时段：{period_name}（{period_range}）
当前时间：{current_time}
今日基调：{day_mood}
节日：{holiday}

角色设定：
{persona}

之前已经发生的时段（只能承接，不能改写，也不要重复写一遍）：
{previous_periods}

可参考的最近生活：
{history_states}

饭点信息：
{meal_section}

{slack_section}

强制约束：
1. 只输出一个可被 JSON 解析的对象，不要输出 Markdown、解释或多余文本。
2. 只写「{period_name}」这一个时段，不要写其他时段，不要写明天。
3. summary 必须是一句中文，长度 18~32 个字，只写这个时段正在做的具体事；不要写成大段描写，不要罗列几件事。
4. mood 用 1~2 个汉字概括心情。
5. location 只能从「家里、公司、学校、路上、店里、公园、河边、外地、不明」中选一个。
6. emoji 只选一个能代表这个时段的符号；不要用表情脸（😊🙂😔）。
7. carry_over 是最多 2 条、每条不超过 12 字的后续线索，供下一个时段承接；没有就输出空列表。
8. 不要写具体吃了什么菜、主食、饮品、水果或点心；只有饭点信息里给了菜名时，才最多提一个菜名，且不要展开做菜过程。
9. 不要输出 outfit、服装、穿搭等字段。
10. 不要写睡觉、起床、几点睡、几点醒；这个时段之外的事不要交代。

输出格式：
{{
  "date": "{date}",
  "period": "{period_name}",
  "mood": "闲适",
  "summary": "在窗边翻着没看完的书，偶尔抬头看看外面的天色",
  "location": "家里",
  "emoji": "📖",
  "carry_over": ["书还剩几页"]
}}

额外要求：
{extra_requirements}
"""


@dataclass(slots=True)
class GenerationResult:
    succeeded: bool
    state: LifeState | None
    error: str | None = None
    used_previous_state: bool = False
    auto_retries_exhausted: bool = False
    skipped: bool = False
    period: str | None = None


def _render_template(template: str, **kwargs: str) -> str:
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: kwargs.get(match.group(1), match.group(0)),
        template,
    )


def _load_default_prompt_template() -> str:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8-sig"))
        template = schema.get("prompt_template", {}).get("default")
        if isinstance(template, str) and "{period_name}" in template:
            return template
    except (OSError, json.JSONDecodeError):
        pass
    return _FALLBACK_PROMPT


class Generator:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        data_mgr: DataManager,
        data_dir: Path | None = None,
    ) -> None:
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self._lock = asyncio.Lock()
        self._auto_failures: dict[str, int] = {}
        self._meal = MealLink(data_dir, config)
        self._generating = False

    @property
    def is_generating(self) -> bool:
        return self._generating

    # ===== 配置 =====

    def _cfg_int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            return bool(self.config.get(key, default))
        except Exception:
            return default

    def _window(self) -> tuple[int, int]:
        return timeline_window_from_config(self.config)

    def _period_names(self) -> list[str]:
        return period_names(self._window())

    def _period_range(self, name: str) -> tuple[int, int] | None:
        return period_range(name, self._window())

    def _scheduled_minute(self, target_date: str, name: str) -> int:
        value = self._period_range(name)
        if not value:
            return 0
        slack = self._cfg_int("period_slack_max_minutes", 35, 0, 90)
        digest = hashlib.sha1(f"{target_date}:{name}".encode("utf-8")).hexdigest()
        return value[0] + int(digest[:8], 16) % (slack + 1)

    def _scheduled_text(self, target_date: str, name: str) -> str:
        return format_clock(self._scheduled_minute(target_date, name))

    def period_is_started(
        self, target_date: str, name: str, now: datetime.datetime
    ) -> bool:
        """该时段是否已经到了（按计划开始时间 + 抖动）。"""
        return minute_of_day(now) >= self._scheduled_minute(target_date, name)

    def _max_retries(self) -> int:
        return self._cfg_int(
            "period_max_retries", DEFAULT_PERIOD_MAX_RETRIES, 1, 5
        )

    def _timeout(self) -> int:
        return self._cfg_int(
            "generation_timeout_seconds", DEFAULT_GENERATION_TIMEOUT, 20, 300
        )

    # ===== 日期与时段状态 =====

    def ensure_day(
        self, target: datetime.date, now: datetime.datetime | None = None
    ) -> LifeState:
        target_date = target.isoformat()
        state = self.data_mgr.get_current(target_date)
        if state is not None:
            changed = self._ensure_periods(state, target_date, now)
            if changed:
                state.rebuild_timeline()
                self.data_mgr.set(state)
            return state

        self.data_mgr.archive_before_generation(target_date)
        state = LifeState(
            date=target_date,
            holiday=self._holiday_name(target),
            status="partial",
            rolling=True,
            generated_at=(now or datetime.datetime.now(CHINA_TIMEZONE)).isoformat(),
        )
        self._ensure_periods(state, target_date, now)
        state.rebuild_timeline()
        self.data_mgr.set(state)
        logger.info(f"[DynamicLifeState] 已建立 {target_date} 的滚动时段表")
        return state

    def _ensure_periods(
        self,
        state: LifeState,
        target_date: str,
        now: datetime.datetime | None = None,
    ) -> bool:
        changed = False
        current = now or datetime.datetime.now(CHINA_TIMEZONE)
        now_minute = minute_of_day(current)
        if not state.periods and state.timeline:
            for entry in state.timeline:
                name = period_for_entry(entry, self._window())
                if not name or name in state.periods:
                    continue
                value = self._period_range(name)
                # 旧版静态时间线不能把还没到的安排当成已发生。
                if value and now_minute < value[0]:
                    continue
                state.periods[name] = PeriodState(
                    status="done",
                    scheduled_at=entry.time,
                    generated_at=state.generated_at,
                    mood=entry.extra_fields.get("mood", ""),
                    summary=entry.schedule,
                    location=entry.extra_fields.get("location", ""),
                    emoji=entry.extra_fields.get("emoji", ""),
                    attempts=1,
                )
                changed = True
            state.rolling = True

        for name in self._period_names():
            period = state.periods.get(name)
            if period is None:
                state.periods[name] = PeriodState(
                    scheduled_at=self._scheduled_text(target_date, name)
                )
                changed = True
            elif period.status == "generating":
                period.status = "pending"
                changed = True
        if changed:
            state.rolling = True
        return changed

    @staticmethod
    def _holiday_name(target: datetime.date) -> str:
        try:
            return holidays.CN(years=[target.year]).get(target, "无") or "无"
        except Exception:
            return "无"

    def _sync_periods(self, state: LifeState) -> None:
        state.rebuild_timeline()

    # ===== 自动生成 =====

    async def generate_due(
        self, now: datetime.datetime | None = None
    ) -> GenerationResult:
        current = now or datetime.datetime.now(CHINA_TIMEZONE)
        day_start = day_start_from_config(self.config.get("day_start", "06:00"))
        target = (current - datetime.timedelta(minutes=day_start)).date()
        state = self.ensure_day(target, current)
        now_minute = minute_of_day(current)
        last = GenerationResult(
            succeeded=True, state=state, skipped=True, period=None
        )
        made = 0
        limit = self._cfg_int("catch_up_limit", DEFAULT_CATCH_UP_LIMIT, 1, 4)

        for name in self._period_names():
            value = self._period_range(name)
            if not value:
                continue
            if now_minute < value[0]:
                break
            period = state.periods.get(name)
            if period is None or period.status != "pending":
                continue
            if now_minute < self._scheduled_minute(target.isoformat(), name):
                break
            if made >= limit:
                break
            result = await self.generate_period(
                target, name, now=current, auto=True
            )
            last = result
            made += 1
            if result.state is not None:
                state = result.state
        return last

    async def generate(
        self,
        target: datetime.date,
        force: bool = False,
        extra: str = "",
        lock_before_minute: int | None = None,
        now: datetime.datetime | None = None,
    ) -> GenerationResult:
        """兼容旧命令入口。/life new 只重试当前时段的未完成状态。"""
        current = now or datetime.datetime.now(CHINA_TIMEZONE)
        state = self.ensure_day(target, current)
        now_minute = minute_of_day(current)
        current_name = period_for_minute(now_minute, self._window())

        chosen: str | None = None
        if force:
            if current_name:
                period = state.periods.get(current_name)
                if period and period.status != "done":
                    chosen = current_name
        else:
            for name in self._period_names():
                period = state.periods.get(name)
                value = self._period_range(name)
                if period and value and now_minute >= value[0]:
                    if period.status == "pending":
                        chosen = name
                        break
            if chosen is None and current_name:
                period = state.periods.get(current_name)
                if period and period.status == "pending":
                    chosen = current_name

        if chosen is None:
            return GenerationResult(
                succeeded=True, state=state, skipped=True, period=None
            )
        return await self.generate_period(
            target, chosen, now=current, force=force, extra=extra
        )

    async def generate_period(
        self,
        target: datetime.date,
        name: str,
        *,
        now: datetime.datetime | None = None,
        force: bool = False,
        extra: str = "",
        auto: bool = False,
    ) -> GenerationResult:
        target_date = target.isoformat()
        current = now or datetime.datetime.now(CHINA_TIMEZONE)
        state = self.ensure_day(target, current)
        period = state.periods.get(name)
        if period is None or period.status == "done":
            return GenerationResult(
                succeeded=True, state=state, skipped=True, period=name
            )
        if self._generating:
            return GenerationResult(
                succeeded=True,
                state=state,
                skipped=True,
                period=name,
                error="already_generating",
            )

        self._generating = True
        try:
            async with self._lock:
                return await self._generate_period_locked(
                    state, target, name, current, force=force, extra=extra, auto=auto
                )
        finally:
            self._generating = False

    async def _generate_period_locked(
        self,
        state: LifeState,
        target: datetime.date,
        name: str,
        now: datetime.datetime,
        *,
        force: bool,
        extra: str,
        auto: bool,
    ) -> GenerationResult:
        target_date = target.isoformat()
        period = state.periods.get(name)
        if period is None or period.status == "done":
            return GenerationResult(
                succeeded=True, state=state, skipped=True, period=name
            )

        attempted = 0
        last_error = ""
        max_retries = self._max_retries()
        while attempted < max_retries:
            attempted += 1
            period.status = "generating"
            period.attempts += 1
            period.error = ""
            self.data_mgr.set(state)
            try:
                prompt = await self._build_prompt(
                    state, target, name, now, extra
                )
                if self._cfg_bool("debug_mode", False):
                    logger.info(f"[DynamicLifeState] Period prompt:\n{prompt}")
                text = await self._call_llm(prompt, target_date, name)
                payload = self._extract_json(text)
                if not isinstance(payload, dict):
                    raise ValueError("模型输出不是 JSON 对象")
                self._apply_payload(period, payload, target_date, name, now)
                state.generated_at = now.isoformat()
                self._sync_periods(state)
                self.data_mgr.set(state)
                logger.info(
                    f"[DynamicLifeState] {target_date} {name} 已生成："
                    f"{period.summary}"
                )
                return GenerationResult(
                    succeeded=True, state=state, period=name
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    f"[DynamicLifeState] {target_date} {name} 第 {attempted} 次"
                    f"生成失败：{exc}"
                )
                if attempted < max_retries:
                    await asyncio.sleep(min(1.5 * attempted, 4.0))

        period.status = "failed"
        period.error = last_error[:300]
        self.data_mgr.set(state)
        if auto:
            self._auto_failures[target_date] = (
                self._auto_failures.get(target_date, 0) + 1
            )
        return GenerationResult(
            succeeded=False,
            state=state,
            error=last_error,
            auto_retries_exhausted=auto,
            period=name,
        )

    async def _call_llm(self, prompt: str, target_date: str, name: str) -> str:
        provider = await self._get_provider()
        if not provider:
            raise RuntimeError("没有可用的 LLM 提供商")
        sid = f"dynamic_life_state_{target_date}_{name}"
        try:
            temperature = float(self.config.get("llm_temperature", 0.8))
        except (TypeError, ValueError):
            temperature = 0.8

        async def _call():
            try:
                return await provider.text_chat(
                    prompt, session_id=sid, temperature=temperature
                )
            except TypeError:
                logger.info(
                    "[DynamicLifeState] provider.text_chat 不接受 temperature，"
                    "回退为默认调用"
                )
                return await provider.text_chat(prompt, session_id=sid)

        resp = await asyncio.wait_for(_call(), timeout=self._timeout())
        text = self._extract_completion_text(resp)
        if not text:
            raise ValueError("模型返回为空")
        return text

    def _apply_payload(
        self,
        period: PeriodState,
        payload: dict,
        target_date: str,
        name: str,
        now: datetime.datetime,
    ) -> None:
        summary = self._clean_summary(payload.get("summary", ""))
        if len(summary) < 6:
            raise ValueError(f"summary 过短：{summary!r}")
        mood = self._clean_mood(payload.get("mood", ""))
        location = self._clean_location(payload.get("location", ""))
        emoji = self._clean_emoji(payload.get("emoji", ""), mood)
        carry = self._clean_carry(payload.get("carry_over", []))

        period.status = "done"
        period.generated_at = now.isoformat()
        period.summary = summary
        period.mood = mood
        period.location = location
        period.emoji = emoji
        period.carry_over = carry
        period.error = ""
        if not period.scheduled_at:
            period.scheduled_at = self._scheduled_text(target_date, name)

    @staticmethod
    def _clean_summary(value: object) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip().strip("“”\"'")
        for sep in ("。", "！", "？", "!", "?"):
            if sep in text:
                text = text.split(sep, 1)[0].strip()
                break
        if len(text) > 32:
            head = text[:32]
            cut = max(head.rfind("，"), head.rfind(","), head.rfind("；"))
            if cut >= 14:
                head = head[:cut]
            text = head.rstrip("，,；; ")
        return text

    @staticmethod
    def _clean_mood(value: object) -> str:
        text = re.sub(r"\s+", "", str(value or ""))
        text = re.sub(r"[^\u4e00-\u9fff]", "", text)
        return text[:2] or "平静"

    @staticmethod
    def _clean_location(value: object) -> str:
        text = str(value or "").strip()
        if text in _ALLOWED_LOCATIONS:
            return text
        for keyword, label in _LOCATION_KEYWORDS:
            if keyword in text:
                return label
        return "不明"

    @staticmethod
    def _clean_emoji(value: object, mood: str) -> str:
        text = str(value or "").strip()
        if text and not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text):
            text = ""
        if not text:
            return mood_emoji(mood)
        if len(text) > 8:
            text = text[:8]
        return text

    @staticmethod
    def _clean_carry(value: object) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        result: list[str] = []
        for item in value:
            text = re.sub(r"\s+", " ", str(item or "")).strip()
            if text:
                result.append(text[:12])
            if len(result) >= 2:
                break
        return result

    # ===== 提示词 =====

    async def _build_prompt(
        self,
        state: LifeState,
        target: datetime.date,
        name: str,
        now: datetime.datetime,
        extra: str,
    ) -> str:
        window = self._window()
        value = self._period_range(name)
        if not value:
            raise ValueError(f"未知时段：{name}")
        template = self._choose_template()
        persona = await self._get_persona() or "一个普通的年轻人"
        previous = self._format_previous(state, name)
        history = self._format_history(state, target)
        meal = self._meal.section(name, target)
        slack = self._build_slack_section(target, name)
        extra_text = (extra or "").strip() or "无"

        prompt = _render_template(
            template,
            date=target.isoformat(),
            period_name=name,
            period_range=f"{format_clock(value[0])}-{format_clock(value[1])}",
            current_time=now.strftime("%Y-%m-%d %H:%M"),
            day_mood=self._day_mood(target),
            holiday=state.holiday or "无",
            persona=persona,
            previous_periods=previous,
            history_states=history,
            meal_section=meal,
            extra_requirements=extra_text,
            slack_section=slack,
            timeline_start=format_clock(window[0]),
            timeline_end=format_clock(window[1]),
        )
        if self._cfg_bool("extra_randomness", True):
            prompt += _EXTRA_RANDOMNESS_SUFFIX
        return prompt

    def _choose_template(self) -> str:
        try:
            configured = str(self.config.get("prompt_template", "") or "")
        except Exception:
            configured = ""
        if "{period_name}" in configured and "{period_range}" in configured:
            return configured
        if configured.strip():
            logger.info(
                "[DynamicLifeState] 检测到旧版整天提示词，已切换为滚动时段提示词"
            )
        return _load_default_prompt_template()

    def _day_mood(self, target: datetime.date) -> str:
        if not self._cfg_bool("day_mood_enable", True):
            return "平常的一天"
        raw = self.config.get("day_mood_pool", "")
        if isinstance(raw, (list, tuple)):
            pool = [str(item).strip() for item in raw if str(item).strip()]
        else:
            pool = [
                item.strip()
                for item in re.split(r"[,，\n]", str(raw))
                if item.strip()
            ]
        if not pool:
            return "平常的一天"
        digest = hashlib.sha1(
            f"day_mood:{target.isoformat()}".encode("utf-8")
        ).hexdigest()
        return pool[int(digest[:8], 16) % len(pool)]

    def _format_previous(self, state: LifeState, current_name: str) -> str:
        lines: list[str] = []
        for name in NATURAL_SLOT_NAMES:
            if name == current_name:
                break
            period = state.periods.get(name)
            if period and period.status == "done" and period.summary:
                carry = (
                    "；后续：" + "、".join(period.carry_over)
                    if period.carry_over
                    else ""
                )
                lines.append(
                    f"- {name}（{period.mood or '平静'}｜"
                    f"{period.location or '不明'}）：{period.summary}{carry}"
                )
        return "\n".join(lines) or "（这是今天第一个时段，没有更早的已完成时段。）"

    def _format_history(self, state: LifeState, target: datetime.date) -> str:
        try:
            limit = int(self.config.get("history_reference_days", 3))
        except (TypeError, ValueError):
            limit = 3
        limit = max(0, min(limit, 7))
        if limit == 0:
            return "无"
        try:
            states = self.data_mgr.get_recent_history(target.isoformat(), limit)
        except Exception:
            return "无"
        lines: list[str] = []
        for old in states:
            summaries = [
                f"{name}：{period.summary}"
                for name in NATURAL_SLOT_NAMES
                if (period := old.periods.get(name))
                and period.status == "done"
                and period.summary
            ]
            if summaries:
                lines.append(f"- {old.date}｜" + "；".join(summaries))
        return "\n".join(lines) or "无"

    def _build_slack_section(
        self, target: datetime.date, name: str
    ) -> str:
        try:
            ratio = float(self.config.get("slack_ratio", 0.3))
        except (TypeError, ValueError):
            ratio = 0.3
        ratio = max(0.0, min(ratio, 1.0))
        digest = hashlib.sha1(
            f"slack:{target.isoformat()}".encode("utf-8")
        ).hexdigest()
        if int(digest[:8], 16) / 0xFFFFFFFF >= ratio:
            return ""
        picked = SLOT_ORDER[int(digest[8:10], 16) % len(SLOT_ORDER)]
        if picked != name:
            return ""
        return (
            "【今日状态】这个时段可以稍微松一点：不必安排得满满当当，"
            "允许发呆、放空或临时改主意；但不要故意写得颓废或戏剧化。"
        )

    async def _get_persona(self) -> str:
        persona_id = str(self.config.get("persona_id", "")).strip()
        if persona_id:
            try:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    return self._extract_persona_prompt(persona)
                logger.warning(
                    f"[DynamicLifeState] persona_id={persona_id} 不存在，"
                    "已回退默认人格"
                )
            except Exception as exc:
                logger.warning(
                    f"[DynamicLifeState] 读取人格 {persona_id} 失败：{exc}"
                )
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            return self._extract_persona_prompt(persona) if persona else ""
        except Exception as exc:
            logger.warning(f"[DynamicLifeState] 读取默认人格失败：{exc}")
            return ""

    @staticmethod
    def _extract_persona_prompt(persona: object) -> str:
        if isinstance(persona, dict):
            return persona.get("system_prompt") or persona.get("prompt", "")
        return (
            getattr(persona, "system_prompt", None)
            or getattr(persona, "prompt", "")
            or ""
        )

    async def _get_provider(self):
        provider_id = str(self.config.get("llm_provider_id", "")).strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
            logger.warning(
                f"[DynamicLifeState] llm_provider_id={provider_id} 未找到，"
                "回退当前默认提供商"
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
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
