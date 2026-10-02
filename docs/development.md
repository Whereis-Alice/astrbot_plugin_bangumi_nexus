# 开发与维护

[← 返回首页](../README.md)

```
astrbot_plugin_bangumi_nexus/
├── main.py                 指令与函数工具入口，只做「事件 ↔ 服务」翻译
├── nexus/
│   ├── config.py           配置解析与校验
│   ├── http.py             统一 HTTP 客户端：重试 / 缓存 / 并发闸门 / 统计
│   ├── store.py            SQLite 持久层（全异步）
│   ├── titles.py           标题归一化、季度换算、放送周期解析
│   ├── catalog.py          指令目录（帮助卡与管理页共用同一份数据）
│   ├── sources/            8 个数据源适配器，一个源一个文件
│   ├── services/           业务层：搜索 / 追番 / 订阅 / 通知 / 调度 / Webhook / ani-rss / 体检
│   ├── render/             6 主题 + 15 套 HTML 模板 + Pillow 兜底 + 降级引擎
│   └── web/                管理页后端与独立 Webhook 监听
├── pages/nexus/            管理页前端（原生 JS，无构建步骤）
├── tests/                  纯函数单测（标题归一化 / 季度换算 / RSS 解析 / 配置强转 …）
└── assets/                 Logo、卡片预览图、管理页截图
```

几条内部约定，改代码前值得知道：

- **服务层不依赖 AstrBot SDK。** `nexus/services/` 只接收一个 `Deps` 容器、只返回 `Reply` 对象，
  由 `main.py` 翻译成消息链。所以服务层可以脱离 AstrBot 单独跑测试，`tests/` 里的用例就是这么干的。
- **指令目录只有一份。** 帮助卡、管理页的指令表、[完整指令](commands.md) 里的指令表以 `catalog.py` 为准；测试检查文档是否覆盖全部指令。
- **主题变量只有一份。** `render/themes.py` 里每个主题 28 个 CSS 变量，卡片模板和管理页样式表共用，
  管理页的 `theme.css` 由 `scripts/build_theme_css.py` 生成。
- **注释解释「为什么」，不解释「是什么」。** 代码本身应该已经说清楚做了什么。

## 跑单测

```bash
pip install -r requirements-dev.txt
python -m ruff format .
python -m ruff check .
python -m pytest
```

单测**完全离线**：需要 HTML 的用例都读 `tests/fixtures/` 下的静态样本，不会访问任何站点，
所以断网、被墙、上游改版都不会让它变红。覆盖的是最容易悄悄坏掉的部分 —— 标题归一化与季度换算、
RSS / Webhook 载荷解析、配置强转、SQLite 持久层、管理页 API，以及「帮助卡上的指令是否真的注册了」这类一致性检查。

欢迎 issue 与 PR。报 bug 时请附上 `/番剧诊断` 的结果和相关日志片段。

## 文档维护

- 首页只保留简介、安装、常用操作与文档导航，不堆放版本号、修复历史或完整参数表。
- 指令与配置参考分别维护在 `docs/commands.md` 和 `docs/configuration.md`；新增功能时同步对应专题文档。
- 更新记录只写入 [CHANGELOG](../CHANGELOG.md)。
- 图片路径从 `docs/` 引用时使用 `../assets/`。修改后运行文档测试，检查相对链接、锚点、指令和配置项覆盖。
