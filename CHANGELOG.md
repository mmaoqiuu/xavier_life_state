# 更新日志

## v1.1.0
* 新增：`/life new` 指令，用于强制重新生成今日安排（main.py、core/generator.py）
  - 读取当前北京时间（Asia/Shanghai），已经开始过的时段原样保留，只重写之后尚未开始的部分
  - 正在进行中的时段同样视为已经发生，不参与重写，避免角色当前活动被中途改写
  - 双保险：提示词把当前时间和已发生时段告知模型，插件本地再强制合并一次，模型不遵守也不会改动过去
  - 当天时段全部已开始时直接跳过模型调用并给出提示，不再浪费一次请求
  - 原 `/life regen` 保留为兼容别名，行为与 `/life new` 完全一致
* 新增：提示词占位符 `{current_time}` 与 `{locked_section}`，默认模板已使用，自定义模板可选用
* 新增：当天天气展示（core/weather.py）
  - /life 输出在日期下方新增一行「☁️ 天气：多云 23°C」，emoji + 天气描述 + 实时温度
  - 数据源 Open-Meteo 实时天气（免密钥），WMO 天气码映射约 28 种天气与昼夜图标（夜间晴为 🌙）
  - 城市可配置（weather_city，默认成都），常见城市内置坐标，其余走地理编码服务
  - 缓存策略：内存 + 磁盘（data 目录 weather_cache.json），TTL 60 分钟，避免频繁请求
  - 容错：超时 8 秒，接口异常或解析失败时静默降级为不显示该行，不影响 /life 任何输出
  - 仅当目标日期为今天时展示天气，查询历史状态不附带天气
  - LLM 工具 get_full_dynamic_life_state 返回值同步附带天气，角色可自然感知当天天气
* 修改：/life full 输出移除「今日概况」「今日氛围」两行，展示更精简
  - 数据层与对话注入逻辑不变，角色氛围与日程底色仍照常生成与使用
* 修改：「实际生成时间」文案简化为「生成时间」
* 修改：_conf_schema.json 新增 weather_enable / weather_city；requirements.txt 新增 aiohttp
* 修改：版本号由 0.9.0 重置为 1.1.0，作为首个对外发布版本
* 原因：让重新生成只影响未来、不再改写已经发生的日程，状态更连贯；同时让状态展示更贴近真实一天的体感，并保持输出清爽、渲染不阻塞、外部接口异常不影响主流程

## v0.8.0
* 新增：节日与日程/符号深度联动
  - 模板新增约束 10：节日非「无」时日程必须自然融入该节日氛围与生活变化，为「无」时禁止编造
  - 节日符号表改为只描述符号含义（🌕 圆月 / 🧧 红封 / 🏮 花灯…），交由模型根据角色人设自主挑选与发挥，避免套用模板清单
* 新增：团圆类节日在对话侧注入想念底色（core/injector.py）
  - 涵盖中秋、除夕、春节、元宵、七夕、情人节、圣诞、元旦、跨年、冬至等节日
  - 注入层引导角色自然浮现想念与牵挂，含蓄留白，不刻意生硬提节日
  - 想念仅作为对话的情绪底色，不写入客观日程表
* 修复：移除 _conf_schema.json 与配置文件中的 UTF-8 BOM 头，解决 AstrBot 仪表盘加载插件报「Unexpected UTF-8 BOM」的问题
* 原因：丰富节日氛围感与角色陪伴感，同时保持日程与情感分层清晰、系统加载稳定

## v0.7.0

## v0.6.0
* 新增：心情 emoji 自动匹配（core/state.py）
  - 新增 MOOD_EMOJI_TABLE 映射表与 mood_emoji() 函数，共 12 类心情
    😄 愉快 / 😌 闲适 / 😪 困倦 / 🥰 温柔 / 🥺 委屈 / 🤔 专注
    😔 低落 / 😕 烦躁 / 😬 紧张 / 😠 生气 / 😐 无聊 / 🤩 期待
  - 关键词按长度优先匹配，避免「不耐烦」被「烦」抢先命中、「慵困」被「困」抢走
  - 未命中或心情为空时回退到 🙂
* 修改：时间线渲染格式改为「【时段】emoji 心情 | 描述」
  - 时段标记由半角 [深夜] 改为全角【深夜】
  - 取消 [23:00, 24:00) 具体区间展示，只保留时段标记
  - 心情由独立一行合并至描述前，emoji 随心情变化
* 修改：/life state 输出同步改为单行格式，移除「🕐 当前时段」「⌛ 时段范围」两行
* 移除：main.py 中已无调用方的 _format_match_intervals()，及随之无用的
  format_interval、resolve_entry_intervals 导入
* 修正：确认 metadata.yaml 的 display_name 保持为「兔的一天」
  - v0.5.1 曾记录为改为「xavier的一天」，实际后续已改回，本次同步文档与文件一致
* 说明：仅调整对外展示层；注入给模型的 <life_state> 文本、时段匹配逻辑、
  数据存储格式与生成模板均未改动
* 原因：去掉机械的时段区间标注，让状态呈现更接近角色自己的独白口吻

## v0.5.1
* 修改：display_name 由「兔的一天」改为「xavier的一天」
* 修改：desc 描述文案，与 xavier_life_state 的新定位对齐
* 新增：short_desc 字段（此前缺失）
* 原因：插件更名后同步对外的展示信息

## v0.5.0
* 移除：今日吃食模块整体下线，本插件不再承担任何与吃饭相关的职责
  - 删除 core/meal/（menu.py / pool.py / store.py / amap.py / __init__.py）
  - 删除 foods_default.py 默认食物库
  - core/injector.py：移除 meal_text 参数与 <today_meal> 注入块
  - main.py：llm_tool 文档说明不再提及今日吃食
  - 提示词模板：删除全部食物、饮食相关约束与措辞
* 移除：数据文件 foods.json、meal_history.json、favorite_foods.json.bak
* 变更：插件 ID 由 astrbot_plugin_dynamic_life_state 改为 xavier_life_state
  - 插件目录、配置文件名、数据目录同步迁移，life_state.json 与原配置保留
* 变更：指令组 dls 简化为 life
  - /dls show → /life state
  - /dls full → /life full
  - /dls regen → /life regen
* 保留：生活时间线生成与注入、时段匹配、/life 系列指令、会话隔离
* 原因：插件定位收窄为「只生成日程表」，不参与吃食管理

## v0.4.0
* 移除：今日吃食模块中的「心头好」融合
  - core/meal/menu.py：删除 _reference_blocks() 及其在生成提示词中的拼接
  - core/meal/pool.py：删除 _load_favorites_as_foods()，候选池不再合并心头好
  - core/meal/store.py：删除 favorite_foods.json 读写方法
* 移除：今日吃食模块中的「附近外卖」融合
  - core/meal/menu.py：删除附近的读取与缓存逻辑
  - core/meal/pool.py：删除 _load_nearby_as_foods()
  - core/meal/store.py：删除 nearby_foods_cache.json 读写方法
* 移除：实例属性 _nearby_loaded 及相关过期注释
* 保留：生活时间线生成与注入、食物库、菜单历史去重、网页重摇
* 原因：不再使用这两套功能；原实现中心头好为默认开启，属静默生效
