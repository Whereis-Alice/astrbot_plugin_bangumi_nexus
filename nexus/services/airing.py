"""放送期核验：星期表只是候选，空结束日期不等于尚未完结。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from ..models import DataItem, Episode, Subject
from ..titles import JST, parse_broadcast, parse_datetime
from .base import Deps

CHECK_TIMEOUT = 20.0
CHECK_CONCURRENCY = 5
END_FIELDS = ("播放结束", "放送结束", "放映结束", "放送終了", "放送終了日", "播放終了")


def exact_date(text: str) -> date | None:
    """只有精确到日的日期才能判完结，季度、月份、日期范围都不猜。"""

    value = str(text or "").strip()
    match = re.fullmatch(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", value)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _broadcast_date(value: date, item: DataItem | None) -> date:
    """深夜档的分集日期常写前一晚，末集当天必须按卡片的归组日保留。

    只修正日本凌晨档相差一天的情况，不把任意日期强行挪到下个星期。
    """

    slot = parse_broadcast(item.broadcast) if item else None
    if (
        slot
        and slot.start.astimezone(JST).hour < 5
        and (slot.air_weekday - value.isoweekday()) % 7 == 1
    ):
        return value + timedelta(days=1)
    return value


def metadata_status(
    subject: Subject, item: DataItem | None, today: date, *, include_data_end: bool = True
) -> str:
    """Bangumi 日期优先，bangumi-data 补缺；末集当天仍属于放送期。"""

    start = exact_date(subject.air_date)
    if start is None and item:
        begin = parse_datetime(item.begin)
        start = begin.astimezone(JST).date() if begin else None
    if start and start > today:
        return "upcoming"
    if subject.platform.lower() in {"剧场版", "movie", "ova", "oad"}:
        return "excluded"
    end = next(
        (day for key in END_FIELDS if (day := exact_date(subject.infobox.get(key, "")))), None
    )
    if end is not None:
        return "ended" if _broadcast_date(end, item) < today else "airing"
    if item and include_data_end:
        end_at = parse_datetime(item.end)
        if end_at:
            return "ended" if end_at.astimezone(JST).date() < today else "airing"
    return "unknown"


def episode_status(
    subject: Subject, episodes: Sequence[Episode], item: DataItem | None, today: date
) -> str:
    """有完整正片编号和日期才判完结，不能把最后一个已录入的分集当最终话。

    总集数用 total_episodes；eps 可能只是已录入条数，不能作为完结证据。
    季内编号优先，连续编号缺 ep 时不通过数组下标推算。
    """

    dated = [(ep, exact_date(ep.airdate)) for ep in episodes]
    if any(day and _broadcast_date(day, item) >= today for _, day in dated):
        return "airing"
    total = subject.total_episodes
    numbered = {
        int(ep.number): day
        for ep, day in dated
        if ep.number > 0 and float(ep.number).is_integer() and day is not None
    }
    if total > 0 and all(number in numbered for number in range(1, total + 1)):
        return "ended"
    # 上周播过不代表本周还播：缺少未来分集和结束日期时，补充栏应等待资料确认。
    return "unknown"


class AiringFilter:
    """核验主栏与补充栏；有界并发与总时限保证接口故障不拖住每日播报。"""

    def __init__(self, deps: Deps, *, now: datetime | None = None) -> None:
        self._deps = deps
        self._today = (now or datetime.now(JST)).astimezone(JST).date()

    async def filter(
        self, subjects: Sequence[Subject], *, supplemental: bool = False
    ) -> tuple[Subject, ...]:
        """主日历未知时保留官方候选；补充栏需要在播证据，不能复活旧季番。"""

        if not subjects:
            return ()
        gate = asyncio.Semaphore(CHECK_CONCURRENCY)
        decisions: dict[int, str] = {}

        async def one(subject: Subject) -> None:
            try:
                async with gate:
                    decisions[subject.id] = await self._status(subject)
            except Exception:  # noqa: BLE001 - 核验不可用时按未知处理，不假定完结
                decisions[subject.id] = "unknown"

        tasks = [asyncio.create_task(one(subject)) for subject in subjects]
        try:
            await asyncio.wait(tasks, timeout=CHECK_TIMEOUT)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        accepted = {"airing"} if supplemental else {"airing", "unknown"}
        result = []
        for subject in subjects:
            state = decisions.get(subject.id, "unknown")
            if state in accepted:
                result.append(subject)
            else:
                self._deps.activity.info(
                    "airing", f"放送筛除 {subject.id} {subject.display_name}：{state}"
                )
        return tuple(result)

    async def _status(self, subject: Subject) -> str:
        """详情和分集通过共享 HTTP TTL 缓存复用；不另存永久完结状态。"""

        source = self._deps.hub.bangumi_data
        lookup = getattr(source, "cached_by_bangumi_id", None)
        item = lookup(subject.id) if callable(lookup) else None
        state = metadata_status(subject, item, self._today, include_data_end=False)
        if state != "unknown":
            return state
        try:
            detailed = await self._deps.hub.bangumi.subject(subject.id)
        except Exception:  # noqa: BLE001 - 详情抓取失败仍可检查已有元数据和分集
            detailed = None
        if isinstance(detailed, Subject):
            subject = detailed
        state = metadata_status(subject, item, self._today)
        if state != "unknown":
            return state
        episodes = await self._deps.hub.bangumi.episodes(subject.id, limit=200)
        return episode_status(subject, episodes, item, self._today)
