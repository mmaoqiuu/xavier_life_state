import asyncio
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
from .core.nap import NapManager
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
    day_start_from_config,
    entries_in_window,
    find_slot_by_name,
    format_clock,
    get_enabled_fields,
    minute_of_day,
    mood_emoji,
    parse_clock_time,
    period_for_minute,
    period_range,
    select_current_slot,
    timeline_window_from_config,
)


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
        self.nap = NapManager(self.data_dir, self.config)
        self._chat_edit_day = ""
        self._chat_edit_count = 0
        self._chat_edit_last_at = 0.0
        self._chat_edit_recent: dict[str, float] = {}

    async def initialize(self) -> None:
        self.data_mgr = DataManager(self.state_file)
        self.generator = Generator(
            self.context, self.config, self.data_mgr, self.data_dir
        )
        try:
            await self.weather.initialize()
        except Exception:
            logger.exception("[DynamicLifeState] 天气服务初始化失败")
        self._start_scheduler()
        self._catch_up_task = asyncio.create_task(self._startup_catch_up())

    async def terminate(self) -> None:
        task = getattr(self, "_catch_up_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._stop_scheduler()
        try:
            await self.weather.terminate()
        except Exception:
            logger.exception("[DynamicLifeState] 天气服务关闭失败")

    # ===== Scheduler =====

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(CHINA_TIMEZONE)

    def _day_start(self) -> int:
        """生活日起点（分钟）。"""
        return day_start_from_config(self.config.get("day_start", "06:00"))

    def _window(self) -> tuple[int, int]:
        """日程窗口（起点分钟, 终点分钟）。"""
        return timeline_window_from_config(self.config)

    def _current_date(self, now: datetime.datetime | None = None) -> datetime.date:
        """按「生活日」计算日期：day_start 之前算前一天。

        例如 day_start=06:00 时，09-29 01:00 属于 09-28 这一天。
        """
        value = now or self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=CHINA_TIMEZONE)
        else:
            value = value.astimezone(CHINA_TIMEZONE)
        return (value - datetime.timedelta(minutes=self._day_start())).date()

    def _window_footer(self, now: datetime.datetime) -> str:
        """当前时间不在日程窗口内时给出一条说明，否则返回空串。"""
        start, end = self._window()
        if start > end:
            return ""
        minute = minute_of_day(now)
        scope = f"日程 {format_clock(start)}-{format_clock(end)}"
        if minute < start:
            return f"（当前不在{scope}之内，今天的日程还没开始。）"
        if minute >= end:
            return f"（当前不在{scope}之内，今天的日程已经收尾。）"
        return ""

    def _select_current_state(
        self, now: datetime.datetime
    ) -> tuple[LifeState | None, datetime.date]:
        target_date = self._current_date(now)
        state = self.data_mgr.get_current(target_date.isoformat())
        if state is None:
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
            day_start = self._day_start()
            self._scheduler.add_job(
                self._daily_generate,
                "cron",
                hour=day_start // 60,
                minute=day_start % 60,
                id="dynamic_life_state_daily",
            )
            self._scheduler.add_job(
                self._period_tick,
                "interval",
                minutes=5,
                id="dynamic_life_state_period_tick",
            )
            self._scheduler.add_job(
                self._nap_settle,
                "interval",
                minutes=10,
                id="dynamic_life_state_nap_settle",
            )
            self._scheduler.start()
            logger.info(
                f"[DynamicLifeState] Scheduler started at "
                f"{format_clock(day_start)} {CHINA_TIMEZONE.key}"
            )
        except Exception as e:
            logger.error(f"[DynamicLifeState] Scheduler startup failed: {e}")

    def _stop_scheduler(self) -> None:
        try:
            if hasattr(self, "_scheduler") and self._scheduler.running:
                self._scheduler.shutdown()
        except Exception:
            pass

    async def _startup_catch_up(self) -> None:
        """启动后补齐已经到点但还没生成的时段。"""
        try:
            await asyncio.sleep(3)
            now = self._now()
            logger.info("[DynamicLifeState] 启动补生成检查")
            await self.generator.generate_due(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[DynamicLifeState] Startup catch-up failed")

    async def _daily_generate(self) -> None:
        """跨天时只建立空白时段表，具体内容到点再生成。"""
        now = self._now()
        target_date = self._current_date(now)
        try:
            self.generator.ensure_day(target_date, now)
        except Exception:
            logger.exception("[DynamicLifeState] 建立今日时段表失败")

    async def _period_tick(self) -> None:
        """每 5 分钟检查一次：只补已经到点的时段。"""
        if self.generator.is_generating:
            return
        try:
            await self.generator.generate_due(self._now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[DynamicLifeState] 时段自动生成失败")

    async def _nap_settle(self) -> None:
        """定时把已经过了终点的午睡结算掉，顺便把数据桥刷到当天。"""
        try:
            self.nap.settle_if_due(self._now())
        except Exception:
            logger.exception("[DynamicLifeState] nap settle failed")

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

            self._cleanup_previous_injection(req)
            now = self._now()
            if self._handle_nap(req, event, now):
                return
            self._append_schedule_edit_hint(req, event.message_str)
            if not self._should_inject(event.message_str):
                return

            data, target_date = self._select_current_state(now)
            if data is None:
                data = self.generator.ensure_day(target_date, now)

            window = self._window()
            current_name = period_for_minute(minute_of_day(now), window)
            current_period = data.periods.get(current_name) if current_name else None
            if (
                current_period is not None
                and current_period.status != "done"
                and self.generator.period_is_started(
                    target_date.isoformat(), current_name, now
                )
                and not self.generator.is_generating
            ):
                result = await self.generator.generate_period(
                    target_date, current_name, now=now
                )
                if result.state is not None:
                    data = result.state

            current_match = None
            if current_name and data.periods:
                period = data.periods.get(current_name)
                if period is not None and period.status == "done":
                    current_match = select_current_slot(
                        data.timeline, minute_of_day(now), window
                    )
            elif data.timeline:
                current_match = select_current_slot(
                    data.timeline, minute_of_day(now), window
                )

            injection_method = str(
                self.config.get("injection_mode", "extra_user_content_parts")
            )
            injection_method = self._resolve_injection_method(req, injection_method)

            if injection_method == "extra_user_content_parts":
                inject_text = build_injection_text(
                    data,
                    current_match,
                    now,
                    window=window,
                    day_start=self._day_start(),
                )
                req.extra_user_content_parts.append(
                    TextPart(text=inject_text).mark_as_temp()
                )
            elif injection_method == "fake_tool_call":
                fake_messages = build_fake_tool_call(
                    data,
                    current_match,
                    now,
                    window=window,
                    day_start=self._day_start(),
                )
                req.contexts.extend(fake_messages)

        except Exception:
            logger.exception("[DynamicLifeState] on_llm_request failed")

    def _handle_nap(
        self, req: ProviderRequest, event: AstrMessageEvent, now: datetime.datetime
    ) -> bool:
        """午睡期间接管本轮请求；返回 True 表示已经处理，不再注入日程。"""
        if not self.nap.enabled():
            return False
        nap = self.nap.active(now)
        if nap is None:
            return False

        if self.nap.is_wake_request(event.message_str):
            self.nap.mark_wake(now)
            logger.info("[DynamicLifeState] 午睡被叫醒，本轮不注入日程")
            return True

        prompt = self.nap.otto_prompt()
        clock = now.strftime("%H:%M")
        req.system_prompt = f"{prompt}\n\n现在是 {clock}。"
        hosting_text = (
            "<otto_hosting>\n"
            "沈星回正在睡午觉，你暂时作为 Otto 替他看消息、记下留言。\n"
            f"他大概 {nap.get('end', '')} 醒来。\n"
            "</otto_hosting>"
        )
        req.extra_user_content_parts.append(
            TextPart(text=hosting_text).mark_as_temp()
        )
        return True

    def _append_schedule_edit_hint(
        self, req: ProviderRequest, message_str: str
    ) -> None:
        """命中约定类关键词时，给主模型一条临时提示。"""
        if not bool(self.config.get("chat_schedule_edit_enable", True)):
            return
        if not self._is_schedule_candidate(message_str):
            return
        hint = (
            "<schedule_edit_hint>\n"
            "这句话可能在改变今天的安排。若对方确实和你说定了今天某个时段的事，"
            "请调用 update_life_schedule 改你自己的行程；只是随口聊天或不确定就不要调用。"
            "改完不要主动说「我改行程了」，正常接话即可。\n"
            "</schedule_edit_hint>"
        )
        req.extra_user_content_parts.append(
            TextPart(text=hint).mark_as_temp()
        )

    def _is_schedule_candidate(self, message_str: str) -> bool:
        raw = self.config.get(
            "chat_schedule_edit_keywords",
            [
                "一起", "约", "见面", "来找", "等你", "等我",
                "今晚", "晚上", "中午", "下午", "上午", "明天",
                "改", "取消", "不去了", "推迟", "提前", "加班", "出门",
            ],
        )
        if isinstance(raw, str):
            raw = [raw]
        elif not isinstance(raw, (list, tuple)):
            raw = []
        keywords = [
            str(item).strip().casefold()
            for item in raw
            if str(item).strip()
        ]
        if not keywords:
            return False
        text = str(message_str or "").casefold()
        return any(keyword in text for keyword in keywords)

    # ===== Session =====

    def _should_inject(self, message_str: str) -> bool:
        raw = self.config.get("injection_trigger_keywords", ["在干嘛"])
        if isinstance(raw, str):
            raw = [raw]
        elif not isinstance(raw, (list, tuple)):
            raw = []

        keywords = [str(item).strip().casefold() for item in raw if str(item).strip()]
        if not keywords:
            return True

        text = str(message_str or "").casefold()
        return any(keyword in text for keyword in keywords)

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

    @filter.llm_tool(name="update_life_schedule")
    async def update_life_schedule(
        self,
        event: AstrMessageEvent,
        period: str,
        description: str,
        mode: str = "replace",
    ) -> str:
        """修改沈星回今天某个时段的行程。

        period 只能是 上午 / 中午 / 下午 / 晚上。
        description 是 10~30 字的行程描述，replace 和 cancel 都必填。
        mode 为 replace（改安排）或 cancel（取消原安排，并写清取消后这个时段做什么）。
        只在对方明确和你约定了今天某个时段的事时调用；随口聊天不要调用。
        """
        if not self._is_session_enabled(event.unified_msg_origin):
            return "当前会话未启用动态生活状态功能。"
        if not bool(self.config.get("chat_schedule_edit_enable", True)):
            return "聊天改行程功能未开启。"
        if self.generator.is_generating:
            return "生活状态正在生成中，请稍后再试。"
        try:
            ok, message = self._apply_schedule_edit(
                period,
                description,
                mode,
                now=self._now(),
                enforce_limits=True,
            )
        except Exception:
            logger.exception("[DynamicLifeState] update_life_schedule failed")
            return "改行程失败，请稍后再试。"
        return message if ok else f"没有改成：{message}"

    def _apply_schedule_edit(
        self,
        period: str,
        description: str,
        mode: str,
        *,
        now: datetime.datetime,
        enforce_limits: bool,
    ) -> tuple[bool, str]:
        """把一句话写进今天的某个时段；返回 (是否成功, 说明)。"""
        period = str(period or "").strip()
        if period not in NATURAL_SLOT_NAMES:
            return False, "时段只能是上午、中午、下午、晚上。"

        mode = str(mode or "replace").strip().lower()
        if mode in ("cancel", "取消"):
            mode = "cancel"
        else:
            mode = "replace"

        desc = self._clean_schedule_text(description)
        if mode == "cancel" and not desc:
            desc = "对方取消了，在家待着"
            logger.warning(
                "[DynamicLifeState] cancel 模式缺少 description，使用兜底文案"
            )
        if not desc:
            return False, "行程描述不能为空。"
        if mode == "cancel" and len(desc) < 8:
            desc = f"对方取消了，{desc}"
        if len(desc) > 32:
            desc = desc[:32].rstrip("，。,.；; ")
        if len(desc) < 8:
            return False, "行程描述太短了，请写清楚这个时段做什么（8~32 字）。"

        if self._contains_food(desc):
            if mode == "cancel":
                desc = "对方取消了，在家待着"
            else:
                desc = "和对方一起吃饭，其余时间照常"

        bounds = period_range(period, self._window())
        if bounds is None:
            return False, "当前日程窗口里没有这个时段。"
        start, end = bounds
        now_minute = minute_of_day(now)
        if now_minute >= end:
            return False, f"{period}已经过去了，不能改。"

        target_date = self._current_date(now)
        state = self.data_mgr.get_current(target_date.isoformat())
        if state is None:
            state = self.generator.ensure_day(target_date, now)
        period_state = state.periods.get(period)
        if period_state is None:
            return False, f"今天的{period}还没有建立，请稍后再试。"

        if enforce_limits:
            limit_error = self._check_chat_edit_limits(
                now, target_date, period, desc
            )
            if limit_error:
                return False, limit_error

        started = start <= now_minute < end
        live_changed = started and period_state.status == "done"
        if live_changed:
            if period_state.live_note == desc:
                return False, "这个时段已经改成这个安排了。"
            period_state.live_note = desc
            period_state.live_note_at = now.isoformat()
        else:
            period_state.summary = desc
            period_state.status = "done"
            period_state.generated_at = now.isoformat()
            period_state.live_note = ""
            period_state.live_note_at = ""
            period_state.carry_over.clear()
            period_state.error = ""
            if not period_state.mood:
                period_state.mood = "平静"
            if not period_state.location:
                period_state.location = "不明"
            if not period_state.emoji:
                period_state.emoji = "✨"

        state.rebuild_timeline()
        self.data_mgr.set(state)
        if enforce_limits:
            self._record_chat_edit(now, period, desc)

        if live_changed:
            return True, f"已记下{period}的临时变动：{desc}"
        if mode == "cancel":
            return True, f"已取消{period}的原安排，改成：{desc}"
        return True, f"已更新{period}的行程：{desc}"

    def _check_chat_edit_limits(
        self,
        now: datetime.datetime,
        target_date: datetime.date,
        period: str,
        desc: str,
    ) -> str | None:
        day_key = target_date.isoformat()
        if self._chat_edit_day != day_key:
            self._chat_edit_day = day_key
            self._chat_edit_count = 0
            self._chat_edit_recent.clear()

        try:
            max_per_day = int(self.config.get("chat_schedule_edit_max_per_day", 6))
        except (TypeError, ValueError):
            max_per_day = 6
        if max_per_day > 0 and self._chat_edit_count >= max_per_day:
            return f"今天改行程的次数已经用完了（最多 {max_per_day} 次）。"

        try:
            min_interval = float(
                self.config.get("chat_schedule_edit_min_interval", 15)
            )
        except (TypeError, ValueError):
            min_interval = 15
        stamp = now.timestamp()
        if (
            min_interval > 0
            and self._chat_edit_last_at
            and stamp - self._chat_edit_last_at < min_interval
        ):
            wait = max(1, int(min_interval - (stamp - self._chat_edit_last_at)))
            return f"改得太频繁了，请等 {wait} 秒再试。"

        recent_key = f"{period}|{desc}"
        last = self._chat_edit_recent.get(recent_key)
        if last is not None and stamp - last < 1800:
            return "这个时段刚刚已经改成一样的内容了。"
        return None

    def _record_chat_edit(
        self, now: datetime.datetime, period: str, desc: str
    ) -> None:
        self._chat_edit_count += 1
        self._chat_edit_last_at = now.timestamp()
        self._chat_edit_recent[f"{period}|{desc}"] = now.timestamp()
        cutoff = now.timestamp() - 1800
        self._chat_edit_recent = {
            key: value
            for key, value in self._chat_edit_recent.items()
            if value >= cutoff
        }

    @staticmethod
    def _clean_schedule_text(raw: str) -> str:
        text = str(raw or "").strip()
        text = re.sub(r"[\r\n]+", "，", text)
        text = re.sub(r"\s+", " ", text)
        text = text.replace("用户说", "").replace("用户", "")
        text = text.replace("聊天里", "").replace("聊天中", "")
        text = re.sub(r"^[\s，,。；;：:]+|[\s，,。；;：:]+$", "", text)
        text = re.sub(r"[，,。；;、]{2,}", "，", text)
        return text.strip(" \t\"'“”‘’")

    @staticmethod
    def _contains_food(text: str) -> bool:
        foods = (
            "火锅", "烧烤", "奶茶", "咖啡", "蛋糕", "甜点", "水果",
            "饮料", "米饭", "面条", "饺子", "包子", "零食", "汉堡",
            "披萨", "炸鸡", "粥",
        )
        return any(item in text for item in foods)

    # ===== Formatting =====

    @staticmethod
    def _format_timeline_entry(
        entry: TimelineEntry, enabled: dict, live_note: str = ""
    ) -> str:
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
        if live_note:
            line += f"（临时变动：{live_note}）"
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
        footer: str = "",
        state: LifeState | None = None,
    ) -> str:
        """渲染当前时段：天气 + 条目 + 可选脚注。"""
        lines: list[str] = []
        if weather_line:
            lines.append(weather_line)
        enabled = get_enabled_fields(self.config)
        live_note = ""
        if state is not None:
            period = state.periods.get(match.entry.time)
            if period is not None:
                live_note = period.live_note
        lines.append(
            self._format_timeline_entry(match.entry, enabled, live_note)
        )
        if footer:
            lines.append(footer)
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
        for entry in entries_in_window(state.timeline, self._window()):
            period = state.periods.get(entry.time)
            live_note = period.live_note if period is not None else ""
            lines.append(
                self._format_timeline_entry(entry, enabled, live_note)
            )
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

    @life.command("nap")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_nap(
        self, event: AstrMessageEvent, action: str = ""
    ) -> AsyncGenerator[object, None]:
        now = self._now()
        action = action.strip().lower()
        if action in ("test", "on"):
            data = self.nap.force_now(now, 30)
            yield event.plain_result(
                f"已切到午睡：{data.get('start')}-{data.get('end')}，Otto 上线。"
            )
            return
        if action == "off":
            self.nap.cancel_today(now)
            yield event.plain_result("今天取消午睡。")
            return
        yield event.plain_result(self.nap.describe(now))

    @life.command("set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_set(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[object, None]:
        extra = _extract_args_after(event.message_str, "life set")
        if not extra:
            yield event.plain_result(
                "用法：/life set <时段> <内容>\n"
                "示例：/life set 晚上 和对方约好一起看电影"
            )
            return
        period, _, description = extra.partition(" ")
        period = period.strip()
        description = description.strip()
        if period not in NATURAL_SLOT_NAMES:
            yield event.plain_result(
                "时段只能是：上午、中午、下午、晚上。"
            )
            return
        if not description:
            yield event.plain_result("请补上这个时段要做什么。")
            return
        if self.generator.is_generating:
            yield event.plain_result("状态正在生成中，请稍后再试。")
            return
        now = self._now()
        try:
            ok, message = self._apply_schedule_edit(
                period,
                description,
                "replace",
                now=now,
                enforce_limits=False,
            )
        except Exception:
            logger.exception("[DynamicLifeState] /life set failed")
            yield event.plain_result("改行程失败，请稍后再试。")
            return
        if not ok:
            yield event.plain_result(message)
            return
        target_date = self._current_date(now)
        data = self.data_mgr.get_current(target_date.isoformat())
        if data is None:
            yield event.plain_result(message)
            return
        weather_line = await self._weather_line_for(target_date, now)
        yield event.plain_result(
            message + "\n\n" + self._format_full_state(
                data, weather_line=weather_line
            )
        )

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
            data = self.generator.ensure_day(target_date, now)

        window = self._window()
        current_name = period_for_minute(minute_of_day(now), window)
        current_period = data.periods.get(current_name) if current_name else None
        if (
            current_period is not None
            and current_period.status != "done"
            and self.generator.period_is_started(
                target_date.isoformat(), current_name, now
            )
            and not self.generator.is_generating
        ):
            result = await self.generator.generate_period(
                target_date, current_name, now=now
            )
            if result.state is not None:
                data = result.state
                current_period = data.periods.get(current_name)

        window_footer = self._window_footer(now)

        if not time_query:
            if current_period is not None and current_period.status != "done":
                yield event.plain_result(
                    f"当前时段（{current_name}）还没有生成好，"
                    "请稍后再试；已经过去的时段不会受影响。"
                )
                return
            match = (
                select_current_slot(data.timeline, minute_of_day(now), window)
                if current_period is not None
                else None
            )
            if match is None:
                yield event.plain_result(window_footer or "当前没有可用的时段状态。")
                return
            weather_line = await self._weather_line_for(target_date, now)
            yield event.plain_result(
                self._format_current_state(
                    match,
                    weather_line=weather_line,
                    footer=window_footer,
                    state=data,
                )
            )
            return

        if time_query in NATURAL_SLOT_NAMES:
            match = find_slot_by_name(data.timeline, time_query, window)
            if match is None:
                yield event.plain_result("没有可用的时段状态。")
                return
            weather_line = await self._weather_line_for(target_date, now)
            yield event.plain_result(
                self._format_current_state(
                    match,
                    weather_line=weather_line,
                    footer=window_footer,
                    state=data,
                )
            )
            return

        query_minute = parse_clock_time(time_query)
        match = select_current_slot(data.timeline, query_minute, window)
        if match is None:
            yield event.plain_result("没有可用的时段状态。")
            return
        weather_line = await self._weather_line_for(target_date, now)
        yield event.plain_result(
            self._format_current_state(
                match,
                weather_line=weather_line,
                footer=window_footer,
                state=data,
            )
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
            data = self.generator.ensure_day(target_date, now)

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
        target_date = self._current_date(now)
        self.generator.ensure_day(target_date, now)
        lock_minute = minute_of_day(now)
        lock_text = format_clock(lock_minute)

        if extra:
            yield event.plain_result(
                f"正在按附加要求重试当前时段：{extra}"
            )
        else:
            yield event.plain_result("正在重试当前尚未生成的时段...")

        result = await self.generator.generate(
            target_date,
            force=True,
            extra=extra,
            lock_before_minute=lock_minute,
        )

        if result.skipped and result.state is not None:
            weather_line = await self._weather_line_for(target_date, now)
            yield event.plain_result(
                "当前时段已经生成，done 的时段不会被改写。\n"
                "要改今天的内容，请等下一个时段，或先手动删除当天数据。\n\n"
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
                yield event.plain_result("当前时段生成失败，请稍后再试。")
            return

        weather_line = await self._weather_line_for(target_date, now)
        period_text = f"{result.period} " if result.period else ""
        yield event.plain_result(
            f"{period_text}时段重新生成完成；今天其他已完成的时段保持不变。\n\n"
            f"{self._format_full_state(result.state, weather_line=weather_line)}"
        )