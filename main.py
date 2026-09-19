import datetime
import re
from collections.abc import AsyncGenerator

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.generator import Generator
from .core.injector import (
    build_fake_tool_call,
    build_injection_text,
    remove_fake_tool_call_from_context,
)
from .core.weather import WeatherService
from .core.state import (
    CHINA_TIMEZONE,
    NATURAL_SLOT_NAMES,
    DataManager,
    LifeState,
    SlotMatch,
    TimelineEntry,
    find_slot_by_name,
    get_enabled_fields,
    minute_of_day,
    mood_emoji,
    parse_clock_time,
    select_current_slot,
)


def _format_minute(minute: int) -> str:
    hour, minute_of_hour = divmod(minute, 60)
    return f"{hour:02d}:{minute_of_hour:02d}"


def _extract_args_after(message_str: str, command: str) -> str | None:
    text = message_str.lstrip("/").strip()
    text = re.sub(r"\s+", " ", text)
    prefix = f"{command} "
    idx = text.find(prefix)
    if idx == -1:
        return None
    remainder = text[idx + len(prefix):].strip()
    return remainder or None


class DynamicLifeStatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.state_file = self.data_dir / "life_state.json"
        self.weather = WeatherService(self.config, self.data_dir)

    async def initialize(self) -> None:
        self.data_mgr = DataManager(self.state_file)
        self.generator = Generator(self.context, self.config, self.data_mgr)
        try:
            await self.weather.initialize()
        except Exception:
            logger.exception("[DynamicLifeState] 天气服务初始化失败")
        self._start_scheduler()

    async def terminate(self) -> None:
        self._stop_scheduler()
        try:
            await self.weather.terminate()
        except Exception:
            logger.exception("[DynamicLifeState] 天气服务关闭失败")

    # ===== Scheduler =====

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(CHINA_TIMEZONE)

    def _current_date(self, now: datetime.datetime | None = None) -> datetime.date:
        value = now or self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=CHINA_TIMEZONE)
        else:
            value = value.astimezone(CHINA_TIMEZONE)
        return value.date()

    def _select_current_state(
        self, now: datetime.datetime
    ) -> tuple[LifeState | None, datetime.date]:
        target_date = self._current_date(now)
        state = self.data_mgr.get_current(target_date.isoformat())
        if state is None or state.status != "ok":
            return None, target_date
        return state, target_date

    def _start_scheduler(self) -> None:
        try:
            self._scheduler = AsyncIOScheduler(
                timezone=CHINA_TIMEZONE,
                executors={"default": AsyncIOExecutor()},
                job_defaults={
                    "coalesce": True,
                    "max_instances": 1,
                    "misfire_grace_time": 120,
                },
            )
            self._scheduler.add_job(
                self._daily_generate,
                "cron",
                hour=0,
                minute=0,
                id="dynamic_life_state_daily",
            )
            self._scheduler.start()
            logger.info(
                f"[DynamicLifeState] Scheduler started at 00:00 {CHINA_TIMEZONE.key}"
            )
        except Exception as e:
            logger.error(f"[DynamicLifeState] Scheduler startup failed: {e}")

    def _stop_scheduler(self) -> None:
        try:
            if hasattr(self, "_scheduler") and self._scheduler.running:
                self._scheduler.shutdown()
        except Exception:
            pass

    async def _daily_generate(self) -> None:
        now = self._now()
        data, target_date = self._select_current_state(now)
        if data:
            logger.info(f"[DynamicLifeState] State for {target_date} exists; skip")
            return
        await self.generator.generate(target_date)

    # ===== LLM Hook =====

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        try:
            if not self._is_session_enabled(event.unified_msg_origin):
                return

            now = self._now()
            data, target_date = self._select_current_state(now)
            if data is None:
                if self.generator.is_generating:
                    return
                result = await self.generator.generate(target_date)
                if not result.succeeded or result.state is None:
                    return
                data = result.state

            current_match = select_current_slot(data.timeline, minute_of_day(now))

            injection_method = str(
                self.config.get("injection_mode", "extra_user_content_parts")
            )
            injection_method = self._resolve_injection_method(req, injection_method)
            self._cleanup_previous_injection(req)

            if injection_method == "extra_user_content_parts":
                inject_text = build_injection_text(data, current_match, now)
                req.extra_user_content_parts.append(
                    TextPart(text=inject_text).mark_as_temp()
                )
            elif injection_method == "fake_tool_call":
                fake_messages = build_fake_tool_call(data, current_match, now)
                req.contexts.extend(fake_messages)

        except Exception:
            logger.exception("[DynamicLifeState] on_llm_request failed")

    # ===== Session =====

    def _is_session_enabled(self, unified_msg_origin: str) -> bool:
        mode = str(self.config.get("session_list_mode", "whitelist")).strip()
        session_list: list[str] = list(self.config.get("session_list", []) or [])

        if mode == "none":
            return True
        if mode == "whitelist":
            return unified_msg_origin in session_list
        if mode == "blacklist":
            return unified_msg_origin not in session_list
        if getattr(self, "_warned_invalid_session_mode", None) != mode:
            logger.warning(
                f"[DynamicLifeState] Invalid session_list_mode {mode!r}; "
                "falling back to whitelist"
            )
            self._warned_invalid_session_mode = mode
        return unified_msg_origin in session_list

    def _resolve_injection_method(self, req: ProviderRequest, configured: str) -> str:
        if configured != "fake_tool_call":
            return configured
        try:
            provider = self.context.get_using_provider(req.session_id)
            provider_config = getattr(provider, "provider_config", {})
            provider_type = (
                str(provider_config.get("type", ""))
                if isinstance(provider_config, dict)
                else ""
            )
            model = provider.get_model() if hasattr(provider, "get_model") else ""
            if "googlegenai" in provider_type or "gemini" in str(model).lower():
                return "extra_user_content_parts"
        except Exception:
            pass
        return configured

    def _cleanup_previous_injection(self, req: ProviderRequest) -> None:
        remove_fake_tool_call_from_context(req.contexts)

    # ===== LLM Tool =====

    @filter.llm_tool(name="get_full_dynamic_life_state")
    async def get_full_dynamic_life_state(
        self, event: AstrMessageEvent, date: str = ""
    ) -> str:
        """获取 Bot 当前或指定日期的完整生活状态。

        date 参数为可选，格式 YYYY-MM-DD。不传则默认返回今天的状态。
        只返回生活状态（时段/日程/心情/地点）。
        """
        if not self._is_session_enabled(event.unified_msg_origin):
            return "当前会话未启用动态生活状态功能。"

        if self.generator.is_generating:
            return "生活状态正在生成中，请稍后再试。"

        now = self._now()
        if date:
            date = date.strip()
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                return "日期格式错误，请使用 YYYY-MM-DD 格式。"
            target_str = date
            data = self.data_mgr.get_by_date(target_str)
        else:
            data, target_date = self._select_current_state(now)
            if data is None:
                return "当前时间没有已生成的生活状态。"
            target_str = target_date.isoformat()

        if not data:
            return f"{target_str} 的生活状态尚未生成。"

        try:
            target = datetime.date.fromisoformat(target_str)
        except ValueError:
            target = None
        weather_line = (
            await self._weather_line_for(target, now) if target is not None else None
        )
        return self._format_full_state(data, weather_line=weather_line)

    # ===== Formatting =====

    @staticmethod
    def _format_timeline_entry(entry: TimelineEntry, enabled: dict) -> str:
        """渲染单条时间线：[时段] emoji 心情 | 描述。"""
        time_label = (entry.time or "").strip()
        mood = entry.extra_fields.get("mood", "").strip()
        emoji = entry.extra_fields.get("emoji", "").strip()
        if not emoji:
            emoji = mood_emoji(mood) if mood else ""
        head = f"[{time_label}]{emoji}" if emoji else f"[{time_label}]"
        if mood:
            head += f" {mood}"
        line = f"{head} | {entry.schedule or '无'}" if (emoji or mood) else head + (entry.schedule or "无")
        others = [
            f"{spec['icon']} {entry.extra_fields[key]}"
            for key, spec in enabled.items()
            if key not in ("mood", "emoji") and entry.extra_fields.get(key)
        ]
        if others:
            line += " " + " ".join(others)
        return line

    @staticmethod
    def _format_generated_at(raw: str | None) -> str:
        """格式化生成时间为 HH:MM:SS，去掉日期、毫秒和时区。"""
        if not raw:
            return "未知"
        try:
            return datetime.datetime.fromisoformat(raw).strftime("%H:%M:%S")
        except Exception:
            import re
            m = re.search(r"(\d{2}:\d{2}:\d{2})", raw)
            return m.group(1) if m else raw

    def _format_current_state(
        self,
        match: SlotMatch,
        *,
        weather_line: str | None = None,
    ) -> str:
        """渲染当前时段：仅天气与事件（时段由条目自带）。"""
        lines: list[str] = []
        if weather_line:
            lines.append(weather_line)
        enabled = get_enabled_fields(self.config)
        lines.append(self._format_timeline_entry(match.entry, enabled))
        return "\n".join(lines)

    def _format_full_state(
        self, state: LifeState, *, weather_line: str | None = None
    ) -> str:
        lines = [f"📅 日期：{state.date}"]
        if weather_line:
            lines.append(weather_line)
        lines.extend(
            [
                f"🎉 节日：{state.holiday}",
                f"⏰ 生成时间：{self._format_generated_at(state.generated_at)}",
                "",
                "📋 完整时间线：",
            ]
        )
        enabled = get_enabled_fields(self.config)
        for entry in state.timeline:
            lines.append(self._format_timeline_entry(entry, enabled))
        return "\n".join(lines)

    async def _weather_line_for(
        self, target_date: datetime.date, now: datetime.datetime
    ) -> str | None:
        """仅当目标日期为今天时附带天气，历史状态不显示天气。"""
        try:
            if target_date != self._current_date(now):
                return None
            return await self.weather.get_line()
        except Exception:
            logger.exception("[DynamicLifeState] 天气行渲染失败")
            return None

    # ===== /life commands =====

    @filter.command_group("life")
    def life(self) -> None:
        pass

    @life.command("state")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_state(
        self, event: AstrMessageEvent, time_query: str = ""
    ) -> AsyncGenerator[object, None]:
        time_query = time_query.strip()
        if time_query:
            if time_query in NATURAL_SLOT_NAMES:
                pass
            elif ":" in time_query:
                try:
                    parse_clock_time(time_query)
                except ValueError:
                    yield event.plain_result("时间格式错误，请使用 HH:MM。")
                    return
            else:
                allowed = "、".join(NATURAL_SLOT_NAMES)
                yield event.plain_result(
                    f"不支持的时段「{time_query}」，仅支持 HH:MM 或：{allowed}"
                )
                return

        now = self._now()
        if self.generator.is_generating:
            yield event.plain_result("状态正在生成中，请稍后再试。")
            return

        data, target_date = self._select_current_state(now)
        if data is None:
            yield event.plain_result("今天的状态尚未生成，正在生成...")
            result = await self.generator.generate(target_date)
            if not result.succeeded or result.state is None:
                if result.auto_retries_exhausted:
                    yield event.plain_result(
                        "今日自动生成次数已达上限，请使用 /life new 重试。"
                    )
                else:
                    yield event.plain_result("状态生成失败，请稍后再试。")
                return
            data = result.state

        if not data.timeline:
            yield event.plain_result("没有可用的时段状态。")
            return

        if not time_query:
            match = select_current_slot(data.timeline, minute_of_day(now))
            if match is None:
                yield event.plain_result("没有可用的时段状态。")
                return
            weather_line = await self._weather_line_for(target_date, now)
            yield event.plain_result(
                self._format_current_state(match, weather_line=weather_line)
            )
            return

        if time_query in NATURAL_SLOT_NAMES:
            match = find_slot_by_name(data.timeline, time_query)
            if match is None:
                yield event.plain_result("没有可用的时段状态。")
                return
            weather_line = await self._weather_line_for(target_date, now)
            yield event.plain_result(
                self._format_current_state(match, weather_line=weather_line)
            )
            return

        query_minute = parse_clock_time(time_query)
        match = select_current_slot(data.timeline, query_minute)
        if match is None:
            yield event.plain_result("没有可用的时段状态。")
            return
        weather_line = await self._weather_line_for(target_date, now)
        yield event.plain_result(
            self._format_current_state(match, weather_line=weather_line)
        )

    @life.command("full")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_full(
        self, event: AstrMessageEvent, date: str = ""
    ) -> AsyncGenerator[object, None]:
        now = self._now()
        if date:
            date = date.strip()
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                yield event.plain_result("日期格式错误，请使用 YYYY-MM-DD。")
                return
            data = self.data_mgr.get_by_date(date)
            if not data:
                yield event.plain_result(f"{date} 的状态不存在。")
                return
            try:
                weather_line = await self._weather_line_for(
                    datetime.date.fromisoformat(date), now
                )
            except ValueError:
                weather_line = None
            yield event.plain_result(
                self._format_full_state(data, weather_line=weather_line)
            )
            return

        data, target_date = self._select_current_state(now)
        if data is None:
            if self.generator.is_generating:
                yield event.plain_result("状态正在生成中，请稍后再试。")
                return
            yield event.plain_result("今天的状态尚未生成，正在生成...")
            result = await self.generator.generate(target_date)
            if not result.succeeded or result.state is None:
                if result.auto_retries_exhausted:
                    yield event.plain_result(
                        "今日自动生成次数已达上限，请使用 /life new 重试。"
                    )
                else:
                    yield event.plain_result("当前暂无有效状态。")
                return
            data = result.state

        weather_line = await self._weather_line_for(target_date, now)
        yield event.plain_result(
            self._format_full_state(data, weather_line=weather_line)
        )

    @life.command("new")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_new(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[object, None]:
        async for item in self._regenerate(event, "life new"):
            yield item

    @life.command("regen")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_regen_legacy(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[object, None]:
        """兼容旧名称，等价于 /life new。"""
        async for item in self._regenerate(event, "life regen"):
            yield item

    async def _regenerate(
        self,
        event: AstrMessageEvent,
        command: str,
    ) -> AsyncGenerator[object, None]:
        if self.generator.is_generating:
            yield event.plain_result("已有生成任务在进行中，请稍后再试。")
            return

        extra = _extract_args_after(event.message_str, command)
        now = self._now()
        _, target_date = self._select_current_state(now)
        lock_minute = minute_of_day(now)
        lock_text = _format_minute(lock_minute)

        if extra:
            yield event.plain_result(
                f"正在根据附加要求重新生成 {lock_text} 之后的安排：{extra}"
            )
        else:
            yield event.plain_result(
                f"正在重新生成今日安排（{lock_text} 之前已经发生的时段保持不变）..."
            )

        result = await self.generator.generate(
            target_date,
            force=True,
            extra=extra,
            lock_before_minute=lock_minute,
        )

        if result.skipped and result.state is not None:
            weather_line = await self._weather_line_for(target_date, now)
            yield event.plain_result(
                "今天的时间段都已经开始了，没有可以重新生成的部分。\n\n"
                f"{self._format_full_state(result.state, weather_line=weather_line)}"
            )
            return

        if not result.succeeded or result.state is None:
            if result.used_previous_state and result.state is not None:
                weather_line = await self._weather_line_for(target_date, now)
                yield event.plain_result(
                    "重新生成失败，已保留原状态。\n\n"
                    f"{self._format_full_state(result.state, weather_line=weather_line)}"
                )
            else:
                yield event.plain_result("状态生成失败，请稍后再试。")
            return

        weather_line = await self._weather_line_for(target_date, now)
        yield event.plain_result(
            f"重新生成完成，{lock_text} 之前的时段保持不变。\n\n"
            f"{self._format_full_state(result.state, weather_line=weather_line)}"
        )