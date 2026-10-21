# xavier_life_state · 动态生活状态

给角色生成一整天的生活状态，并在对话时把当前时段的状态悄悄注入，让陪伴有真实流动的呼吸感。

## 功能

- **日程窗口**：每份状态对应一个自然日，日程覆盖 `timeline_start`~`timeline_end`（默认 08:00~20:00），窗口之外不生成、不注入
- **自动生成**：每日在 `day_start`（默认 06:00）定时生成；当天状态缺失时，首次对话或查询时自动补上
- **时段匹配**：窗口内固定 4 个时段（上午 / 中午 / 下午 / 晚上），也兼容具体区间（08:00-09:00）与旧数据里的历史时段名
- **每日基调**：生成前随机抽一条基调（可自定义基调池），避免角色天天同一套作息
- **随机度可调**：生成温度、额外随机性提示可调，越高每日差异越大
- **摆烂时段**：按概率随机抽出「允许摆烂」的时段，生成时会赖床、窝着、发呆；已锁定的历史时段不参与
- **历史参考**：生成时可参考最近若干个成功状态，保持生活连续性
- **节日感知**：按中国大陆日历查询当天节日，日程会自然融入节日氛围
- **天气展示**：查询时在日期下方显示当天天气与温度（Open-Meteo，无需密钥，缓存 1 小时）
- **安静注入**：默认以临时附加上下文注入，不写入对话历史；也可切换到伪造工具调用方式
- **只重写未来**：手动重新生成时，已经开始的时段原样保留，只重写之后的安排
- **窗口外留白**：清晨与深夜不属于日程，注入时只给一句轻提示，「还没睡 / 刚被吵醒」交给角色人设自然推进
- **可选字段可配置**：时间线可带心情、emoji、地点、备注，按需开关
- **会话范围可控**：白名单 / 黑名单 / 全部启用三种模式

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/life state` | 管理员 | 查看当前时段的状态 |
| `/life state <时段>` | 管理员 | 查看指定时段，例如 `/life state 上午` |
| `/life state <HH:MM>` | 管理员 | 查看今天指定时间点的状态 |
| `/life full` | 管理员 | 查看今天的完整时间线 |
| `/life full <YYYY-MM-DD>` | 管理员 | 查看已有日期的完整状态，不生成历史 |
| `/life new [额外要求]` | 管理员 | 重新生成今日安排，已开始的时段保持不变，可附加要求 |
| `/life regen [额外要求]` | 管理员 | 同 `/life new` 的别名 |

## LLM Tool

注册 `get_full_dynamic_life_state`：不传参数返回今天完整安排，传入日期则查询对应日期。仅在用户明确询问全天安排、完整日程或历史状态时触发，与自动注入共用会话范围配置。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `session_list_mode` | `whitelist` | 会话名单模式：白名单 / 黑名单 / 全部 |
| `session_list` | `[]` | 会话 UMO 列表，如 `default:GroupMessage:123456`（`/sid` 可查） |
| `llm_provider_id` | 空 | 生成用的模型，留空用默认模型 |
| `persona_id` | 空 | 生活状态生成用的人格，留空跟随默认人设；点击选择，仅影响生成，不改聊天人设 |
| `llm_temperature` | `1.0` | 生成温度，建议 1.2~1.5 提升多样性 |
| `extra_randomness` | `true` | 追加「换句式、但允许与历史相似」的软提示 |
| `slack_ratio` | `0.3` | 摆烂时段概率：每天按日期稳定掷骰，每段独立命中；0 关闭，1 全天摆烂 |
| `day_mood_enable` | `true` | 启用每日基调随机 |
| `day_mood_pool` | 内置 14 条 | 每日基调池，随机抽取，可自行改写 |
| `history_reference_days` | `3` | 参考最近几天历史，0~7 |
| `injection_trigger_keywords` | `["在干嘛"]` | 命中任一关键词才注入；英文不区分大小写，留空恢复每轮注入 |
| `injection_mode` | `extra_user_content_parts` | 注入方式，推荐默认；另一项为 `fake_tool_call` |
| `day_start` | `06:00` | 一天从几点算起，同时决定每日自动生成时刻，建议 05:00~07:00 |
| `timeline_start` | `08:00` | 日程窗口起点，05:00~11:00 |
| `timeline_end` | `20:00` | 日程窗口终点，18:00~23:00 |
| `optional_fields_enabled` | `mood`,`emoji` | 启用的可选字段，可选 mood / emoji / location / note |
| `weather_enable` | `true` | 是否显示当天天气 |
| `weather_city` | `成都` | 天气城市 |
| `prompt_template` | 内置模板 | 自定义生成提示词，占位符：`{date}` `{day_mood}` `{holiday}` `{persona}` `{history_states}` `{extra_requirements}` `{timeline_optional_fields}` `{current_time}` `{locked_section}` |
| `debug_mode` | `false` | 输出生成与注入的调试日志 |

## 说明

- 所有会话共用同一份状态，不按群、用户或人格分别存储
- 日期、定时任务与时段判断统一使用 `Asia/Shanghai`
- 数据存放在 `data/plugin_data/xavier_life_state/`，含 `life_state.json`、`weather_cache.json` 与 `history/`（保留最近 7 天）；旧版本留下的 `night_sleep.json` 已无任何代码读取，保留不影响运行（想清理也可直接删除）
- 插件不读聊天记录，也不接外部日程服务；天气仅用于展示

## 兼容性

- AstrBot v4.26.4+ / Python 3.10+
- 生成需要至少一个可用的文本 LLM Provider

## 许可证

[AGPL-3.0](LICENSE)
