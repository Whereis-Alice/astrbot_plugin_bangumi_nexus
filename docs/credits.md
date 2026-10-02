# 数据源与致谢

[← 返回首页](../README.md)

- [上游插件](#上游插件)
- [功能范围](#功能范围)
- [许可证](#许可证)

这个插件本身不存储、不缓存、不分发任何影音内容，只做信息索引。全部数据来自下列公开来源，版权归各自站点与权利人所有。

| 数据源 | 提供什么 | 地址 |
| --- | --- | --- |
| **Bangumi 番组计划** | 条目、评分、每日放送、在看人数、分集列表 | <https://bgm.tv> |
| **bangumi-data** | 跨站 ID 与多语言标题对照总表（本插件的匹配枢纽），CC0-1.0 | <https://github.com/bangumi-data/bangumi-data> |
| **長門番堂** | 季度新番表：制作组、声优、题材、首播时间 | <http://yuc.wiki> |
| **AGE 动漫** | 推荐位与更新集数 | <https://www.agedm.io> |
| **anime1.me** | 在线观看索引（繁体） | <https://anime1.me> |
| **萌娘百科** | 作品与角色词条摘要，CC BY-NC-SA 3.0 | <https://zh.moegirl.org.cn> |
| **Mikan Project** | 单番字幕组资源 RSS | <https://mikanani.me> |
| **RSSHub** | 万物皆可 RSS | <https://docs.rsshub.app> |

感谢这些站点长期免费提供数据。请合理设置轮询间隔（默认值已经很保守），不要给它们添麻烦。

## 上游插件

番剧中枢是在下面 6 个开源插件的基础上重写与融合而成的，**指令名保持兼容**，但内部实现是全新的。
向原作者致谢：

| 上游插件 | 借鉴了什么 |
| --- | --- |
| [NoFizz/astrbot_plugin_bangumi_calendar](https://github.com/NoFizz/astrbot_plugin_bangumi_calendar) | 每日放送卡片、封面并发抓取与缓存清理策略、`/新番` 指令组 |
| [united-pooh/astrbot_plugin_bangumi](https://github.com/united-pooh/astrbot_plugin_bangumi) | `/bgm` 系列搜索、日文简介翻译、长回复转卡片、`/追番` `/弃坑` `/放送时间` |
| [Yometenma/astrbot_plugin_autobangumi_notify](https://github.com/Yometenma/astrbot_plugin_autobangumi_notify) | AutoBangumi Webhook 事件解析、目标会话拼装、指数退避与去重 |
| [zhist2028/astrbot_plugin_anime1_list](https://github.com/zhist2028/astrbot_plugin_anime1_list) | anime1.me 列表抓取与观看地址解析、`get_anime_list` / `get_watch_url` 函数工具 |
| [FlanChanXwO/astrbot_plugin_rsshub](https://github.com/FlanChanXwO/astrbot_plugin_rsshub) | RSS 订阅指令体系（`/sub` 全家桶）、RSSHub 路由简写 |
| [xco2/astrbot_plugin_anime_gacha](https://github.com/xco2/astrbot_plugin_anime_gacha) | `/抽番` `/查番`、萌娘百科查询、季度数据维护指令（`/今日新番` 现在是 `/today` 的别名） |

## 功能范围

`astrbot_plugin_rsshub` 的 4 个知识库指令（`rsshub_kb_*`）**没有迁移**。它们依赖那个插件自建的一整套向量知识库
与检索管线，和本插件「轻量、少依赖」的取向冲突，硬搬过来会引入一大堆重依赖。

`/sub_export` 用于备份和迁移订阅，不是知识库导入格式。需要知识库问答时，请按 AstrBot 知识库的文档另行配置。

## 许可证

[AGPL-3.0-or-later](../LICENSE)。本项目衍生自上述开源插件，保留相关署名与许可声明；部署修改版并对外提供服务时，请按许可证履行提供对应源代码等义务。
