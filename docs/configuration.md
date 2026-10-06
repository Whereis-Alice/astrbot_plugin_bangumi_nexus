# 安装与配置参考

[← 返回首页](../README.md)

- [安装与环境](#安装与环境)
- [怎样填写通知目标](#怎样填写通知目标)
- [卡片与渲染](#卡片与渲染)
- [网络与缓存](#网络与缓存)
- [搜索与匹配](#搜索与匹配)
- [每日播报](#每日播报)
- [人格转述](#人格转述)
- [RSS 订阅](#rss-订阅)
- [Webhook 接入](#webhook-接入)
- [ani-rss 同步](#ani-rss-同步)
- [在线观看索引](#在线观看索引)
- [消息投递](#消息投递)
- [其它](#其它)

## 安装与环境

需要 AstrBot 4.25 或更高的 4.x 版本，兼容范围以 [插件元数据](../metadata.yaml) 为准。

推荐在 AstrBot 管理面板的插件市场搜索「番剧中枢」。也可以用「从链接安装」填入仓库地址：

```text
https://github.com/Whereis-Alice/astrbot_plugin_bangumi_nexus
```

需要手动部署时，在 AstrBot 的 `data/plugins` 目录克隆仓库，再在管理面板重载插件。
如已安装提供同名指令的番剧插件，请先停用，避免重复响应。

AstrBot 会自动读取 `requirements.txt` 安装：

| 依赖 | 用途 | 缺了会怎样 |
| --- | --- | --- |
| `httpx[socks]` | 所有网络请求、SOCKS 代理支持 | **必需** |
| `beautifulsoup4` | 解析長門番堂 / AGE / 萌娘百科的网页 | 这三个源不可用，其余正常 |
| `feedparser` | RSS 解析（容错最好） | 自动退回 Python 内置 `xml.etree`，绝大多数源仍可解析 |
| `Pillow` | 卡片渲染的本地兜底 | 少一级降级，仍可用 t2i 与纯文本 |

卡片的 HTML 渲染用的是 AstrBot 自带的渲染能力，**不需要你额外装浏览器**。

## 怎样填写通知目标

「会话」就是一个群聊或私聊。优先在管理页选择已有会话，不要猜 ID。
手动填写时使用完整会话标识，例如 `default:GroupMessage:123456`：
`default` 是 AstrBot 中的平台实例 ID，`123456` 换成目标群号；不是所有实例都叫 `default`。

每日播报、Webhook、ani-rss 同步可以分别设置目标，详见 [通知设置](notifications.md)。

全部可以在 Dashboard 的插件配置页或本插件自带的管理页里改。**开箱默认值就能用，下面这些都是可选的调整。**

## 卡片与渲染

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `card_theme` | string | `midnight` | 卡片默认主题。所有卡片（日历 / 条目 / 追番 / 更新 / 抽番 / 帮助）共用这套配色，用 `/番剧中枢 樱绯` 临时预览，或用 `/sub_profile set 樱绯` 保存到当前会话。<br>可选：`midnight`、`aurora`、`sakura`、`blueprint`、`paper`、`sunset` |
| `card_renderer` | string | `auto` | 卡片渲染方式。auto = AstrBot 无头浏览器 → Pillow 本地绘制 → t2i 文字转图 → 纯文本，逐级兜底；选 text 表示永远只发文字。<br>可选：`auto`、`html`、`raster`、`t2i`、`text` |
| `card_width` | int | `860` | 卡片渲染宽度（像素）。760~1200。太窄会挤，太宽在手机上要缩放。 |
| `notify_character_limit` | int | `3` | 推送卡片的主角人数上限，可填 0~6；0 关闭角色区，不影响简介和制作信息。仅显示 Bangumi 标为主角的人物，资料缺失自动略过。 |
| `show_watch_text` | bool | 开 | 在卡片外附上可点击的观看入口、Bangumi 条目及资源详情。适用于查番、加入追番、抽番和更新通知；图片内不显示网址。关闭后不自动附带链接，仍可用 /在线观看 主动查询。 |

## 网络与缓存

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `bangumi_access_token` | string | （空） | Bangumi Access Token。留空也能用（只读接口不强制）。在 https://next.bgm.tv/demo/access-token 生成后填入可提高配额、减少 429。 |
| `user_agent` | string | （空） | 自定义 User-Agent。留空使用插件内置 UA。Bangumi 官方要求 UA 里带上应用标识。 |
| `proxy` | string | （空） | HTTP / SOCKS 代理。形如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080。留空表示直连。 |
| `http_timeout_seconds` | int | `20` | 单次网络请求超时（秒） |
| `http_max_retries` | int | `3` | 网络请求重试次数。指数退避重试，只对超时 / 5xx / 429 生效。 |
| `cache_ttl_minutes` | int | `30` | 数据源缓存时长（分钟）。日历、季度表、番剧列表等慢变数据的内存缓存时长，0 表示不缓存。 |
| `max_concurrency` | int | `5` | 并发请求上限。抓封面、批量查条目时同时最多几个请求，调太高容易被限流。 |

## 搜索与匹配

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `search_max_results` | int | `5` | 搜索最多返回条数 |
| `enable_cross_match` | bool | 开 | 启用跨源信息聚合。开启后一张卡里同时给出 Bangumi 评分、放送时间、anime1 在线观看、長門番堂 制作组与声优、Mikan RSS 地址。关闭则只查 Bangumi，更快。 |
| `translate_summary` | bool | 关 | 日文简介自动翻译成中文。借用 AstrBot 已配置的 LLM 供应商翻译，会消耗 token。 |
| `translate_provider_id` | string | （空） | 翻译使用的 LLM 供应商。留空则使用当前会话正在用的供应商。 |
| `long_reply_as_card` | bool | 开 | 长文本回复自动转成卡片。超过 200 字或含多行的回复改用图片发送，避免刷屏。 |

## 每日播报

`/今日新番` 和定时播报共用放送期核验：优先使用 Bangumi 的首播、结束日期；结束日期缺失时，只有总集数与完整正片分集日期能共同确认完结，才排除该番。最终话当天仍保留，尚未首播的番暂不列入。

「跨季续播」用于补充日历遗漏的在播番，不意味着所有条目都是年番。空结束日期不能作为在播证据；无法核实的补充条目暂不列出，避免显示已完结作品。主日历在核验接口不可用时保留官方候选；分集资料缺失或上游日期错误时仍可能不准确。月表每六小时刷新，年番召回覆盖过去 400 天；不会依据追番进度达到总集数就判断官方完结。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `push_enabled` | bool | 关 | 启用每日新番播报。每天定时把「今日放送」卡片推给下面配置的目标会话。 |
| `push_time` | string | `08:30` | 每日播报时间。24 小时制 HH:MM，可用英文逗号写多个时间点，例如 08:30,20:00。按 Bot 所在机器的本地时区。 |
| `push_targets` | list | （空） | 播报目标会话（UMO）。填写完整会话标识，形如 default:GroupMessage:123456。也可以直接在群里发「日历订阅 开」让本群自助订阅。 |
| `push_max_items` | int | `12` | 播报卡片最多列出的番剧数 |
| `push_sort_by` | string | `score` | 播报排序依据<br>可选：`score`、`doing`、`time`、`name` |
| `push_sort_order` | string | `desc` | 播报排序方向<br>可选：`desc`、`asc` |
| `push_min_score` | float | `0` | 播报评分下限。低于该评分的番剧不进播报；0 表示不过滤。新番前期常常没有评分，设太高会漏。 |
| `push_min_doing` | int | `0` | 播报在看人数下限。0 表示不过滤。 |
| `push_only_watchlist` | bool | 关 | **只播我追的番**。开启后每日播报只保留出现在本会话追番表里的番，其余一律不列；追番表为空时跳过播报而不是发一张空卡。关闭则播当天全部新番。 |

## 人格转述

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `persona_reply_enabled` | bool | 开 | 通知走 AstrBot 人格转述。开启后新番播报 / RSS 更新 / Webhook 通知会先让当前人格用自己的口吻说一句，再附上卡片一起发送。插件只提供任务指令，人格由 AstrBot 自身配置决定。 |
| `persona_id` | string | （空） | 转述用的人格。指定用哪个人格转述新番通知。留空则跟随目标会话当前的默认人格；两者都取不到时只发卡片。 |
| `persona_provider_id` | string | （空） | 人格转述使用的 LLM 供应商。留空则使用目标会话当前正在用的供应商；取不到时自动跳过转述、只发卡片。 |
| `persona_instruction` | text | `请用你自己的口吻，简短自然地向大家转述下面这条番剧通知。保持信息准确，不要编造剧情，不要使用列表和标题，控制在两句话以内。` | 人格转述任务指令。只写「要做什么」，不要在这里定义人格 —— 语气与人设由 AstrBot 的人格设置决定。 |
| `persona_max_chars` | int | `180` | 人格转述最长字数。超出会截断，防止人格发挥过头把卡片挤到看不见。 |
| `persona_fallback_line` | bool | 开 | 转述失败时用兜底文案填「播报」位。人格调用会重试一次；两次都失败（模型限流、网关抖动）时用一句固定文案顶上，保证卡片版式一致。关闭则失败时那一段留空。 |

## RSS 订阅

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `rss_enabled` | bool | 开 | 启用 RSS 订阅轮询 |
| `rss_interval_minutes` | int | `15` | RSS 轮询间隔（分钟）。最小 5 分钟。Mikan / RSSHub 都不喜欢被高频打，15~30 分钟足够。 |
| `rss_max_items_per_poll` | int | `5` | 单次轮询每个订阅最多推送条数。防止某个源一次吐出几十条时刷屏，超出的只记录不推送。 |
| `rss_pick_source` | bool | 开 | 只给番名时先让用户挑字幕组。开启后 `/sub 番名` 会先列出 Mikan 上收录该番的字幕组，回复序号才真正订阅；关闭则沿用关键词搜索源，会收下所有组的发布。 |
| `rss_first_poll_silent` | bool | 开 | 新订阅首次轮询不推送历史。强烈建议开启：否则刚订阅就会把整个 RSS 历史全推一遍。 |
| `rss_history_days` | int | `14` | 推送去重记录保留天数 |
| `global_excludes` | list | 空 | **全局排除项**：所有会话无条件生效，写在这里就不用每个群再设一遍。可填预设名（`合集` / `生肉` / `720p`…）或任意关键词。**该填片源还是交给同集归并，取决于你订的是单组单番还是宽 feed**，[分情况说明见这里](rss.md#全局层到底该填什么)。 |
| `rss_episode_dedup` | bool | 开 | **同一集只推一个版本**。同一集的简繁 / 画质 / 片源多个版本归并成一条，其余静默跳过；`01v2` 这类修订版优先于原版。 |
| `rss_episode_prefer` | list | `简体, 1080p, Baha, MKV, 外挂` | 同集归并的优先顺序，越靠前权重越高。 |
| `rss_episode_dedup_window_hours` | int | `48` | **同集归并的跨轮次时间窗**（小时）。同一集的不同片源常跨天发布，光在单次轮询内归并会导致「今天推 CR、明天再推 Baha」。开着这个窗口，某一集推过之后窗口内再到的其它版本会静默跳过。`0` = 只在单次轮询内归并。 |
| `rss_auto_progress` | bool | 开 | **RSS 推送时自动回填追番进度**。字幕组发布第 N 集并推送成功后，把追番表里对应那部的进度推到第 N 集。只往前推、不超过总集数、一部番只认标题最接近的那条记录；已弃坑的不动。 |
| `rsshub_base` | string | `https://rsshub.app` | RSSHub 实例地址。用于把 /rsshub 路由补全成完整地址。自建实例更稳定。 |
| `mikan_base` | string | `https://mikanani.me` | Mikan Project 地址。用于生成单番 RSS。国内访问不畅时可改成镜像域名。 |

## Webhook 接入

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `webhook_enabled` | bool | 关 | 启用下载器 Webhook 接收。开启后暴露一个 HTTP 接口，ani-rss、AutoBangumi 等下载器可以把「开始下载 / 下载完成 / 缺集 / 订阅完结」等事件推过来。ani-rss 的中文动作名与 emoji 都能识别。 |
| `webhook_path` | string | `bangumi_nexus/notify` | Webhook 路径。独立通道地址是 `http(s)://<主机>:<端口>/<这里的路径>`；使用反代时以配置的域名和路径为准，见 [接入教程](webhook.md)。 |
| `webhook_token` | string | （空） | Webhook 校验令牌。留空表示不校验（不推荐）。填写后请求必须带 X-Webhook-Token 头且完全一致。 |
| `webhook_notify_watchers` | bool | 开 | 启用追番会话联动。开启后，除固定推送目标外，追番表里收录了该番剧的会话也会收到通知；一台下载器服务多个群时可避免互相刷屏。 |
| `webhook_auto_progress` | bool | 开 | 新集入库时自动推进追番进度。收到「下载完成 / 整理入库」事件时，把追番表里对应番剧的进度推到该集。只会往前推，不会回退。和 RSS 链的同名开关（`rss_auto_progress`）保持一致，默认开启。 |
| `webhook_auto_watch` | bool | **开** | **第一次推过来就自动加入追番表**。某部番第一次被下载器推过来、而「Webhook 通知目标」的追番表里还没有它时，自动补一条，并在同一次请求里接着回填进度。已经在表里的不会重复添加（含弃坑条目，不会被拉回来）；Bangumi 搜不到时也会建一条只有名字的记录。关掉则必须先手动 `/追番` 一次，进度才会开始动。 |
| `webhook_port` | int | `0` | Webhook 独立监听端口。0 表示不开启（此时只走 AstrBot 面板的 /api/plug/ 路由，需要面板登录态）。填 1-65535 会额外起一个只处理 Webhook 的极简 HTTP 服务，供 AutoBangumi 直连；开启时必须同时填写 Webhook Token，否则拒绝启动。 |
| `webhook_bind` | string | `0.0.0.0` | Webhook 监听地址。独立监听端口绑定的网卡。与下载器同机时建议改成 127.0.0.1，仅本机可访问；套了 Nginx 反代也应该改成 127.0.0.1。 |
| `webhook_silent_kinds` | list | 空 | **静默事件**：列在这里的事件照常回填追番进度，但不发卡片。ani-rss 同时勾了「开始下载」和「下载完成」时，把「下载完成」填进来就不会一集刷两条。可填中文动作名（`开始下载` / `下载完成` / `缺少集数` / `订阅完结` / `摸鱼检测`）或内部标识（`download_start` / `download_complete` / `rename_complete`）。 |
| `webhook_targets` | list | 空 | **Webhook 专用推送目标**（UMO）。只影响下载器回调这一条链，留空则沿用「每日播报」里的 `push_targets`。想把下载通知全部丢进某个群、又让每日播报留在私聊时填这里，格式与 `push_targets` 相同。 |
| `dedup_window_seconds` | int | `300` | 通知去重时间窗（秒）。同一条通知在窗口内重复到达只发一次。 |

## ani-rss 同步

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `anirss_enabled` | bool | 关 | 启用 ani-rss 同步。把本地 ani-rss（下载器）里的订阅与下载进度同步进插件的追番表，省得在聊天里一部部 `/追番`。**只读不回写**，绝不改动 ani-rss 自己的配置。 |
| `anirss_base` | string | （空） | ani-rss 地址。形如 `http://192.168.1.10:7789`，默认端口 7789，末尾的 `/api` 可省略。AstrBot 与 ani-rss 不在同一台机器时要填局域网 / 公网可达地址。 |
| `anirss_api_key` | string | （空） | ani-rss 设置页里的 API Key，**优先使用**。填了它就不用填账号密码。 |
| `anirss_username` | string | （空） | 没有 API Key 时的退路：用账号密码登录换 token。 |
| `anirss_password` | string | （空） | 与用户名配套。ani-rss 的登录接口是明文提交，公网暴露时请优先用 API Key 并套 HTTPS。 |
| `anirss_sync_interval_minutes` | int | `60` | 自动同步间隔（分钟）。`0` 表示不自动同步，只在发 `/anirss sync` 或在管理页点按钮时同步。上限 1440。 |
| `anirss_sync_targets` | list | （空） | 同步到哪些会话。追番表按会话分开存，所以必须指明同步进哪个会话，形如 `default:GroupMessage:123456`。**同步账目卡也只发这些会话**，不会混进 RSS 更新推送。留空则自动同步不执行。 |
| `anirss_sync_watchlist` | bool | 开 | 同步成追番记录。把 ani-rss 里的每部番写进追番表，并用它的下载集数回填进度（只往前推）。关掉就只做连接与查看，不落库。 |
| `anirss_sync_subscriptions` | bool | 关 | 同时建立 RSS 订阅。ani-rss 自己已经在轮询这些 RSS 了，再让插件订阅一遍会收到重复通知；只有在「下载归 ani-rss、通知归 AstrBot」时才建议开。 |
| `anirss_notify_on_change` | bool | 开 | 同步有变化时播报。新增追番、进度前进或有条目失联时，往上面的目标会话发一张同步账目卡；没变化时静默。 |
| `anirss_verify_tls` | bool | 开 | 校验 ani-rss 的 HTTPS 证书。只有在用自签证书的反代时才需要关掉；走 `http://` 时此项无影响。 |

## 在线观看索引

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `anime1_enabled` | bool | 开 | 启用 anime1.me 在线观看索引。提供「在线看」链接与番剧列表。该站为第三方聚合站，链接仅作索引。 |
| `anime1_refresh_hours` | string | `1,13,22` | anime1 列表刷新时刻。整点小时数，英文逗号分隔，例如 1,13,22。留空表示不自动刷新。 |

## 消息投递

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `default_platform_id` | string | 空 | 默认平台**实例** ID（AstrBot 平台页里每个适配器的唯一名字，通常是 `default`）。留空＝自动识别当前启用的实例，推荐保持留空。填成适配器类型（如 `aiocqhttp`）也没关系，插件会自动纠正并在活动日志里提示。 |
| `send_max_retries` | int | `3` | 推送失败重试次数 |
| `send_retry_delay_seconds` | float | `2` | 推送重试基础延迟（秒）。指数退避：第 N 次等待 延迟 × 2^(N-1)。 |
| `send_concurrency` | int | `3` | 推送并发上限。同时最多给几个会话发消息，太高容易触发平台风控。 |

## 其它

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `gacha_source` | string | `auto` | 抽番数据来源。bangumi = Bangumi 每日放送，稳定且带评分；yuc = 長門番堂季度新番表，多了制作组 / 声优 / 题材，但部分机房连不上；auto = 先 bangumi，拿不到再回落 yuc。<br>可选：`auto`、`yuc`、`bangumi` |
| `webui_enabled` | bool | 开 | 启用 Dashboard 管理页面。关闭后不注册任何 Web 路由，聊天指令不受影响。 |
| `webui_theme` | string | `midnight` | WebUI 默认主题。首次打开页面时使用，之后以页面里选择的主题为准。<br>可选：`midnight`、`aurora`、`sakura`、`blueprint`、`paper`、`sunset` |

> 提示：所有配置改动**立即生效**，不需要重载插件。网络相关的改动（代理 / 超时 / 并发 / 缓存）会热重建 HTTP 客户端，
> 定时相关的改动（播报时间 / RSS 间隔）会重排调度。
