"""ani-rss 本地服务客户端：把用户在 PC 上维护的订阅表读进来。

为什么要接这个：用户日常在本机 「ani-rss」（wushuo894/ani-rss，Java/Spring Boot）里
一部一部点订阅，追番进度也由它跟着下载器走。插件这边再手工 「/追番」 一遍纯属重复劳动。
把 ani-rss 当成一个「事实来源」同步过来，机器人侧就自动拥有同一份追番表与 RSS 源。

协议要点（照 v2.x 源码核实，不是猜的）：

* 所有 「@RestController」 被 「WebMvcConfig.configurePathMatch」 统一加了 「/api」 前缀，
  所以真实端点是 「POST {base}/api/listAni」 而不是 「/listAni」；默认端口 7789。
* 鉴权二选一：
  1. API Key —— 请求头或 query 带 「api-key」/「x-api-key」/「s」，值等于设置里的 apiKey。
     服务端 apiKey 为空时**一律拒绝**，所以填了 key 才走这条路。
  2. 账号密码 —— 「POST /api/login」 拿 token（明文提交，服务端未做 md5），
     之后放在 「Authorization」 头里。服务端对登录有限流并会随机 sleep 0.5~5 秒，
     因此 token 拿到就缓存，只在 403 时重取一次。
* 响应统一包封 「{code, message, data, t}」，「200 <= code < 300」 才算成功。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from ..http import FetchError, HttpClient

#: ani-rss 默认监听端口，用户只填 IP 时补上它。
DEFAULT_PORT = 7789
#: 「weekLabel」 是 1=周日 … 7=周六，转成 Python 的 isoweekday（1=周一 … 7=周日）。
_WEEK_LABEL_TO_ISO = {"1": 7, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
#: 从 「https://bgm.tv/subject/302286」 这类地址里抠条目 ID。
_SUBJECT_RE = re.compile(r"/subject/(\d+)")


class AniRssError(RuntimeError):
    """ani-rss 侧的可读错误。文案直接给用户看，所以不要塞堆栈。"""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_base(raw: str) -> str:
    """把用户随手填的地址补成可用的 base URL。

    允许 「192.168.1.8」「192.168.1.8:7789」「http://nas:7789/」 三种写法：
    缺协议补 http、缺端口补 7789、尾斜杠一律去掉。这样 WebUI 里少一半的填错。
    """

    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    text = text.rstrip("/")
    # 只在「主机段完全没有冒号」时补端口，避免把 「http://[::1]」 之类切坏。
    scheme, _, rest = text.partition("://")
    host, slash, tail = rest.partition("/")
    if ":" not in host and not host.startswith("["):
        host = f"{host}:{DEFAULT_PORT}"
    return scheme + "://" + host + (slash + tail if slash else "")


def subject_id_of(bgm_url: str) -> int:
    """从 ani-rss 的 「bgmUrl」 里解析 Bangumi 条目 ID，解析不出给 0。"""

    match = _SUBJECT_RE.search(bgm_url or "")
    return int(match.group(1)) if match else 0


def iso_weekday(week_label: Any) -> int:
    """「weekLabel」 → isoweekday。认不出返回 0（＝未知，卡片上不显示星期）。"""

    return _WEEK_LABEL_TO_ISO.get(str(week_label or "").strip(), 0)


@dataclass(frozen=True)
class AniEntry:
    """ani-rss 的一条订阅。只保留插件真正会用到的字段。

    上游 「Ani」 有四十多个字段（下载路径、TMDB 元数据、自定义剧集规则……），
    那些属于下载器的职责，同步过来只会让本地表变成垃圾场，所以刻意不收。
    """

    ani_id: str
    title: str
    url: str = ""
    jp_title: str = ""
    bgm_url: str = ""
    cover: str = ""
    season: int = 1
    subgroup: str = ""
    total: int = 0
    progress: int = 0
    score: float = 0.0
    enabled: bool = True
    completed: bool = False
    ova: bool = False
    week_label: str = ""
    match: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    standby: tuple[str, ...] = ()

    @property
    def subject_id(self) -> int:
        return subject_id_of(self.bgm_url)

    @property
    def weekday(self) -> int:
        return iso_weekday(self.week_label)

    @property
    def display_title(self) -> str:
        """展示用标题：中文优先，空了退日文，再空退 ani_id。"""

        return self.title or self.jp_title or self.ani_id

    def summary(self) -> str:
        """一行简述，给同步结果通知和 「/anirss」 卡片复用。"""

        bits = [self.display_title]
        if self.total:
            bits.append(f"{self.progress}/{self.total}")
        elif self.progress:
            bits.append(f"已更新至 {self.progress}")
        if self.subgroup:
            bits.append(self.subgroup)
        if not self.enabled:
            bits.append("已停用")
        elif self.completed:
            bits.append("已完结")
        return " · ".join(bits)


@dataclass
class AniRssSnapshot:
    """一次 「listAni」 的结果。"""

    entries: tuple[AniEntry, ...] = ()
    total: int = 0
    release_dates: tuple[str, ...] = field(default_factory=tuple)

    @property
    def active(self) -> tuple[AniEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def parse_entry(raw: Any) -> AniEntry | None:
    """把一条 「Ani」 JSON 转成 「AniEntry」。缺标题的直接丢掉。"""

    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    jp_title = str(raw.get("jpTitle") or "").strip()
    if not title and not jp_title:
        return None
    return AniEntry(
        ani_id=str(raw.get("id") or "").strip(),
        title=title,
        url=str(raw.get("url") or "").strip(),
        jp_title=jp_title,
        bgm_url=str(raw.get("bgmUrl") or "").strip(),
        # 「image」 是可访问的网络地址，「cover」 是 ani-rss 主机上的本地路径 ——
        # 后者对机器人毫无意义（不在同一台机器上），所以只认前者。
        cover=str(raw.get("image") or "").strip(),
        season=max(1, _as_int(raw.get("season"), 1)),
        subgroup=str(raw.get("subgroup") or "").strip(),
        total=max(0, _as_int(raw.get("totalEpisodeNumber"))),
        progress=max(0, _as_int(raw.get("currentEpisodeNumber"))),
        score=_as_float(raw.get("score")),
        enabled=_as_bool(raw.get("enable"), True),
        completed=_as_bool(raw.get("completed")),
        ova=_as_bool(raw.get("ova")),
        week_label=str(raw.get("weekLabel") or "").strip(),
        match=_as_tuple(raw.get("match")),
        exclude=_as_tuple(raw.get("exclude")),
        standby=_as_tuple(raw.get("standbyRssList")),
    )


def parse_snapshot(data: Any) -> AniRssSnapshot:
    """解析 「listAni」 的 「data」。

    结构是 「{releaseDateList, weekList: [{weekLabel, items: [Ani]}], total}」；
    同一部番只会出现在一个 weekLabel 下，但仍按 「ani_id」 去重 —— 上游若哪天
    改成「按放送日重复列出」，这里也不会同步出两份。
    """

    if not isinstance(data, dict):
        return AniRssSnapshot()
    seen: dict[str, AniEntry] = {}
    order: list[str] = []
    week_list = data.get("weekList")
    if isinstance(week_list, list):
        for bucket in week_list:
            if not isinstance(bucket, dict):
                continue
            label = str(bucket.get("weekLabel") or "").strip()
            items = bucket.get("items")
            if not isinstance(items, list):
                continue
            for raw in items:
                entry = parse_entry(raw)
                if entry is None:
                    continue
                # 桶上的 weekLabel 比条目里的更可靠（上游就是按它分桶的）。
                if label and not entry.week_label:
                    entry = replace(entry, week_label=label)
                key = entry.ani_id or entry.display_title
                if key not in seen:
                    order.append(key)
                seen[key] = entry
    dates = _as_tuple(data.get("releaseDateList"))
    total = _as_int(data.get("total"), len(seen))
    return AniRssSnapshot(
        entries=tuple(seen[key] for key in order),
        total=total or len(seen),
        release_dates=dates,
    )


class AniRssSource:
    """ani-rss 的最小可用客户端：登录、列订阅、触发刷新。

    刻意**不缓存响应**（用 「http.request」 而非 「fetch_json」）：这是局域网里的
    自建服务，用户点「立即同步」就是想看到刚改完的结果，缓存只会让人怀疑功能坏了。
    """

    def __init__(
        self,
        http: HttpClient,
        *,
        base: str = "",
        api_key: str = "",
        username: str = "",
        password: str = "",
        verify_tls: bool = True,
    ) -> None:
        self._http = http
        self._base = normalize_base(base)
        self._api_key = (api_key or "").strip()
        self._username = (username or "").strip()
        self._password = password or ""
        self._verify_tls = verify_tls
        self._token = ""

    @property
    def base(self) -> str:
        return self._base

    @property
    def configured(self) -> bool:
        """地址 + 任一种凭据都给齐了才算配好。"""

        return bool(self._base) and bool(self._api_key or (self._username and self._password))

    def describe(self) -> dict[str, Any]:
        """给 WebUI / 状态卡用的连接信息，绝不回显密钥本身。"""

        return {
            "base": self._base,
            "configured": self.configured,
            "auth": "api_key" if self._api_key else ("password" if self._username else "none"),
            "token_cached": bool(self._token),
            "verify_tls": self._verify_tls,
        }

    # -- 内部：请求与解包 -------------------------------------------------
    def _url(self, path: str) -> str:
        return self._base + "/api/" + path.strip("/")

    async def _post(
        self,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        """发一次 POST 并把包封拆开，返回 「data」。

        「retry_auth」 让 403 有且只有一次重来的机会：token 过期是常态
        （ani-rss 重启就换），但不能无限递归。
        """

        if not self._base:
            raise AniRssError("还没填 ani-rss 地址")
        headers = await self._headers()
        try:
            response = await self._http.request(
                "POST",
                self._url(path),
                headers=headers,
                params=params or None,
                json_body=body if body is not None else {},
                retries=0,
                expect_status=False,
                insecure=not self._verify_tls,
            )
        except FetchError as error:
            raise AniRssError(f"连不上 ani-rss（{error}）") from error
        except OSError as error:
            raise AniRssError(f"连不上 ani-rss（{error}）") from error
        if response.status_code in {401, 403} and retry_auth and not self._api_key:
            self._token = ""
            return await self._post(path, body=body, params=params, retry_auth=False)
        if response.status_code == 404:
            raise AniRssError(
                "ani-rss 返回 404，确认地址填的是 ani-rss 本体端口（默认 7789）而不是反代路径"
            )
        if response.status_code >= 400:
            raise AniRssError(
                f"ani-rss 返回 HTTP {response.status_code}",
                status=response.status_code,
            )
        return self._unwrap(response)

    def _unwrap(self, response: Any) -> Any:
        try:
            payload = response.json()
        except Exception as error:  # 非 JSON 说明打到了别的服务，转成业务错误抛出
            raise AniRssError("ani-rss 没有返回 JSON，地址可能指向了别的服务") from error
        if not isinstance(payload, dict):
            raise AniRssError("ani-rss 返回了预期外的数据结构")
        code = _as_int(payload.get("code"), 200)
        if not 200 <= code < 300:
            message = str(payload.get("message") or "").strip() or f"错误码 {code}"
            if code in {401, 403}:
                self._token = ""
                raise AniRssError(
                    f"ani-rss 拒绝了这次请求：{message}（检查 API Key 或账号密码）", status=code
                )
            raise AniRssError(f"ani-rss 报错：{message}", status=code)
        return payload.get("data")

    async def _headers(self) -> dict[str, str]:
        """按可用凭据组装鉴权头。API Key 优先，因为它不需要额外一次登录往返。"""

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
            return headers
        token = await self.login()
        if token:
            headers["Authorization"] = token
        return headers

    async def login(self) -> str:
        """拿并缓存 token。已有缓存时直接返回，不重复登录。

        为什么要尽量少登录：ani-rss 的 「LoginController」 在校验前会随机
        「sleep 0.5~5 秒」 防爆破，每次同步都登录会让整轮同步凭空多等几秒。
        """

        if self._token:
            return self._token
        if not (self._username and self._password):
            raise AniRssError("没有可用凭据：填 API Key，或者填 ani-rss 的账号密码")
        try:
            response = await self._http.request(
                "POST",
                self._url("login"),
                headers={"Content-Type": "application/json"},
                json_body={"username": self._username, "password": self._password},
                retries=0,
                expect_status=False,
                insecure=not self._verify_tls,
            )
        except (FetchError, OSError) as error:
            raise AniRssError(f"连不上 ani-rss（{error}）") from error
        if response.status_code >= 400:
            raise AniRssError(f"登录失败，HTTP {response.status_code}", status=response.status_code)
        data = self._unwrap(response)
        token = str(data or "").strip()
        if not token:
            raise AniRssError("登录成功但没拿到 token，换用 API Key 更稳")
        self._token = token
        return token

    # -- 对外接口 ---------------------------------------------------------
    async def list_ani(self) -> AniRssSnapshot:
        """拉取全部订阅。"""

        return parse_snapshot(await self._post("listAni"))

    async def ping(self) -> dict[str, Any]:
        """连通性自检：跑一次 「listAni」，顺带回报条目数与服务端时差。"""

        snapshot = await self.list_ani()
        return {
            "ok": True,
            "total": snapshot.total,
            "entries": len(snapshot.entries),
            "active": len(snapshot.active),
            **self.describe(),
        }

    async def refresh_all(self) -> bool:
        """让 ani-rss 立刻重扫一遍所有 RSS。同步前调它能拿到更新的进度。"""

        await self._post("refreshAll")
        return True
