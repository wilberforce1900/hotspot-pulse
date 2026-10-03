"""
stages/collector.py — 阶段1：数据采集。

分工：senior-coder 实现（机械、批量、重 I/O，适合委派），V4 Pro 审定落地。
职责：
  1. 按 Query.data_source 选择适配器（social_api / search / rss / crawler / mock）。
  2. 拉取/轮询原始数据，统一转成 Post。
  3. 幂等去重（(source, external_id)），返回 CollectResult。

密钥约定（开源友好）：
  适配器**绝不**在代码/配置里保存明文密钥；DataSourceConfig.api_key_env 只存
  环境变量名，取值发生在请求时刻、只进请求头、绝不写入日志或 Post.raw。
"""

from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from hotspot_pulse.config import DataSourceConfig
from hotspot_pulse.models import CollectResult, DataSource, Post, Query
from hotspot_pulse.shared import DEFAULT_REGIONS, stable_hash

logger = logging.getLogger(__name__)

# 关联词池（用于生成话题共现信号）
_RELATED_TAGS = ["股价", "芯片", "科技", "政策", "市场", "研发", "供应链", "出口"]

_USER_AGENT = "HotSpotPulse/0.1 (open-source; +https://github.com/wilberforce1900/hotspot-pulse) "
_HTTP_TIMEOUT_SECONDS = 10
_MAX_FEED_BYTES = 5 * 1024 * 1024  # 响应体积上限，防异常大响应拖垮内存

_HTML_TAG = re.compile(r"<[^>]+>")


def _mock_text(tag: str, category: str) -> str:
    """按情绪类别生成确定性模板文本（嵌共享词典词）。"""
    if category == "positive":
        return f"{tag} 传来利好，市场看涨，投资者感到惊喜。"
    if category == "negative":
        return f"{tag} 出现利空，市场担忧，股价可能暴跌。"
    return f"关于{tag}的最新报道，数据显示讨论持续升温。"


def _collect_mock(query: Query, now: datetime) -> list[Post]:
    """mock 数据源：离线生成仿真 Post 流，供预演/测试。

    可复现性：tag 决定随机种子；now 由调用方注入（缺省当前 UTC），
    同一 (tag, now) 两次调用产出完全相同的 Post 流。
    """
    rng = random.Random(stable_hash(query.tag))
    count = rng.randint(300, 500)
    region_pool = query.regions if query.regions else DEFAULT_REGIONS
    # 用户指定的关联词进入采样池：让用户意图传导到共现信号（related_terms 接线）
    related_pool = list(_RELATED_TAGS) + [
        t for t in query.related_terms if t and t not in _RELATED_TAGS
    ]

    window_start = now - timedelta(hours=query.window_hours)

    posts: list[Post] = []
    for i in range(count):
        # 时间偏置：越接近当下越多（模拟话题「升温」）。指数<1 使 rng.random() 向 1 聚集，
        # 让下游趋势预测能学到正斜率，演示增长预测与破点检测正常工作。
        offset_hours = query.window_hours * (rng.random() ** 0.6)
        created_at = window_start + timedelta(hours=offset_hours)
        region = rng.choice(region_pool)

        roll = rng.random()
        if roll < 0.40:
            category = "positive"
        elif roll < 0.75:
            category = "negative"
        else:
            category = "neutral"
        text = _mock_text(query.tag, category)

        num_related = rng.randint(1, 3)
        related = rng.sample(related_pool, k=num_related)
        tags = [query.tag] + related

        posts.append(Post(
            external_id=f"mock-{i}",
            source=DataSource.MOCK,
            created_at=created_at,
            text=text,
            tags=tags,
            region=region,
            raw={"mock": True},
        ))
    logger.info("mock 采集完成：tag=%s 共 %d 条", query.tag, len(posts))
    return posts


# --------------------------------------------------------------------------- #
# RSS / Atom 新闻源适配器（开源默认接口；密钥只走环境变量，可离线降级）
# --------------------------------------------------------------------------- #
def _http_get(url: str, api_key: str | None, params: dict,
              ) -> bytes:
    """拉取订阅源。密钥只放请求头（X-Api-Key），不进 URL/日志/返回值。"""
    query_string = urllib.parse.urlencode(params or {})
    target = f"{url}{'?' + query_string if query_string else ''}"
    headers = {"User-Agent": _USER_AGENT}
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(target, headers=headers)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        data = resp.read(_MAX_FEED_BYTES + 1)
    if len(data) > _MAX_FEED_BYTES:
        raise ValueError(f"响应超过 {_MAX_FEED_BYTES} 字节上限，已中止")
    return data


def _parse_feed(body: bytes, query: Query, now: datetime,
                max_posts: int) -> tuple[list[Post], list[str]]:
    """解析 RSS 2.0 / Atom 订阅源为 Post 列表（纯函数，可离线测试）。

    安全：拒绝含 DTD/ENTITY 声明的 XML（标准库 ElementTree 对内部实体
    炸弹无防护；正规订阅源不需要 DTD）。条目数按 max_posts 截断。
    """
    errors: list[str] = []
    head = body[:4096]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        errors.append("RSS 内容含 DTD/实体声明，已拒绝（防实体炸弹）")
        return [], errors

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        errors.append(f"RSS XML 解析失败：{exc}")
        return [], errors

    entries: list[tuple[str, str, str, datetime | None]] = []  # (id, title, summary, published)
    if root.tag == "rss":  # RSS 2.0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link).strip()
            desc = (item.findtext("description") or "").strip()
            pub = _parse_date(item.findtext("pubDate"))
            entries.append((guid or link or title, title, desc, pub))
    elif root.tag.endswith("feed"):  # Atom
        for entry in root.iter():
            if not entry.tag.endswith("entry"):
                continue
            title = (entry.findtext("title") or "").strip()
            link_el = next((el for el in entry if el.tag.endswith("link")), None)
            link = (link_el.get("href") if link_el is not None else "") or ""
            eid = next((el.text or "" for el in entry if el.tag.endswith("id")), "")
            summary = next((el.text or "" for el in entry
                            if el.tag.endswith(("summary", "content"))), "")
            pub = _parse_date(next((el.text for el in entry
                                    if el.tag.endswith(("published", "updated"))), None))
            entries.append((eid or link or title, title, summary, pub))
    else:
        errors.append(f"无法识别的订阅源根元素：{root.tag}")
        return [], errors

    posts: list[Post] = []
    for eid, title, summary, published in entries[:max_posts]:
        text = html.unescape(_HTML_TAG.sub("", f"{title} {summary}")).strip()
        posts.append(Post(
            external_id=eid,
            source=DataSource.RSS,
            created_at=published or now,
            text=text[:2000],
            tags=[query.tag],
            raw={"feed_title": title[:200]},
        ))
    logger.info("RSS 解析完成：%d 条（feed 根=%s）", len(posts), root.tag)
    return posts, errors


def _parse_date(raw: str | None) -> datetime | None:
    """RFC822/ISO8601 日期尽力解析；失败返回 None（不抛）。"""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw.strip())
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_rss(query: Query, source_cfg: DataSourceConfig | None,
                 now: datetime) -> tuple[list[Post], list[str]]:
    """RSS 适配器：网络失败/未配置一律降级为错误信息，绝不拖垮流水线。"""
    errors: list[str] = []
    if source_cfg is None or not source_cfg.base_url:
        errors.append("RSS 源未配置（config.data_sources.rss.base_url），已跳过采集")
        return [], errors

    api_key = None
    if source_cfg.api_key_env:
        api_key = os.environ.get(source_cfg.api_key_env)
        if not api_key:
            errors.append(f"环境变量 {source_cfg.api_key_env} 未设置，RSS 以匿名方式请求")

    try:
        body = _http_get(source_cfg.base_url, api_key, source_cfg.params)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("RSS 拉取失败：%s", type(exc).__name__)
        errors.append(f"RSS 拉取失败（离线或源不可达）：{type(exc).__name__}")
        return [], errors

    return _parse_feed(body, query, now, source_cfg.max_posts)


# --------------------------------------------------------------------------- #
# GDELT Doc 2.0 适配器（免费、免密钥；查询词会出境到美国 gdeltproject.org）
# --------------------------------------------------------------------------- #
_GDELT_DEFAULT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_GDELT_MAX_RECORDS = 250          # artlist 单次上限（API 侧硬限制）

# 常见 sourcecountry → 中文区域名（与 shared.DEFAULT_REGIONS 口径一致；
# 未收录的国别名原样透传，成为独立区域节点而非被丢弃）
_GDELT_COUNTRY_TO_REGION = {
    "China": "中国", "Taiwan": "中国台湾", "Hong Kong": "中国香港",
    "United States": "美国", "Japan": "日本", "South Korea": "韩国",
    "United Kingdom": "欧洲", "Germany": "欧洲", "France": "欧洲",
    "Italy": "欧洲", "Spain": "欧洲", "Netherlands": "欧洲",
    "Singapore": "东南亚", "Malaysia": "东南亚", "Thailand": "东南亚",
    "Vietnam": "东南亚", "Philippines": "东南亚", "Indonesia": "东南亚",
    "Canada": "北美", "Mexico": "北美",
}


def _parse_gdelt_seendate(raw: str | None) -> datetime | None:
    """GDELT seendate（YYYYMMDDTHHMMSSZ）→ UTC datetime；失败返回 None。"""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_gdelt(body: bytes, query: Query, now: datetime,
                 max_posts: int) -> tuple[list[Post], list[str]]:
    """解析 GDELT artlist JSON 为 Post 列表（纯函数，可离线测试）。"""
    errors: list[str] = []
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"GDELT 响应非 JSON（可能是空结果或限流页）：{exc}")
        return [], errors

    articles = data.get("articles")
    if not isinstance(articles, list):
        errors.append("GDELT 响应缺少 articles 字段")
        return [], errors

    posts: list[Post] = []
    for art in articles[:max_posts]:
        if not isinstance(art, dict):
            continue
        url = (art.get("url") or "").strip()
        title = (art.get("title") or "").strip()
        if not url and not title:
            continue
        country = (art.get("sourcecountry") or "").strip()
        posts.append(Post(
            external_id=url or title,
            source=DataSource.GDELT,
            created_at=_parse_gdelt_seendate(art.get("seendate")) or now,
            text=f"{title} {(art.get('domain') or '').strip()}".strip()[:2000],
            tags=[query.tag],
            region=_GDELT_COUNTRY_TO_REGION.get(country, country) or None,
            raw={"gdelt_domain": (art.get("domain") or "")[:100],
                 "gdelt_language": (art.get("language") or "")[:16]},
        ))
    logger.info("GDELT 解析完成：%d 条", len(posts))
    return posts, errors


def _collect_gdelt(query: Query, source_cfg: DataSourceConfig | None,
                   now: datetime) -> tuple[list[Post], list[str]]:
    """GDELT 适配器：免密钥；window_hours 映射 timespan；失败降级不拖垮流水线。

    隐私注意：查询词（tag）会发送至美国 gdeltproject.org——由用户在配置层
    明确选择本源即视为接受该出境行为。
    """
    errors: list[str] = []
    base_url = (source_cfg.base_url if source_cfg and source_cfg.base_url
                else _GDELT_DEFAULT_URL)
    max_posts = min(source_cfg.max_posts if source_cfg else 250, _GDELT_MAX_RECORDS)

    params = {
        "query": f'"{query.tag}"',
        "mode": "artlist",
        "format": "json",
        "timespan": f"{query.window_hours}h",
        "maxrecords": str(max_posts),
        "sort": "hybridrel",
    }
    if source_cfg and source_cfg.params:
        params.update({str(k): str(v) for k, v in source_cfg.params.items()})

    try:
        body = _http_get(base_url, None, params)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("GDELT 拉取失败：%s", type(exc).__name__)
        errors.append(f"GDELT 拉取失败（离线或源不可达）：{type(exc).__name__}")
        return [], errors

    return _parse_gdelt(body, query, now, max_posts)


def _collect_social_api(query: Query, source_cfg: DataSourceConfig | None) -> list[Post]:
    """社交平台 API 适配器。senior-coder。离线预演：返回空列表。"""
    return []


def _collect_search(query: Query, source_cfg: DataSourceConfig | None) -> list[Post]:
    """搜索引擎/舆情检索适配器。senior-coder。离线预演：返回空列表。"""
    return []


def collect(query: Query, now: datetime | None = None,
            source_cfg: DataSourceConfig | None = None) -> CollectResult:
    """采集主入口：路由到对应适配器并聚合结果（错误隔离 + 幂等去重）。

    now：注入时钟（mock 窗口锚定 / RSS 兜底时间），缺省当前 UTC。
    source_cfg：当前数据源的连接配置（base_url / api_key_env / params / max_posts），
    由 orchestrator 从 cfg.data_sources 取出传入；mock 不需要，rss 必需。
    """
    errors: list[str] = []
    posts: list[Post] = []
    seen: set[tuple[str, str]] = set()
    deduped = 0
    now = now or datetime.now(timezone.utc)

    try:
        ds = query.data_source
        if ds == DataSource.MOCK:
            raw = _collect_mock(query, now)
        elif ds == DataSource.RSS:
            raw, rss_errors = _collect_rss(query, source_cfg, now)
            errors.extend(rss_errors)
        elif ds == DataSource.GDELT:
            raw, gdelt_errors = _collect_gdelt(query, source_cfg, now)
            errors.extend(gdelt_errors)
        elif ds == DataSource.SOCIAL_API:
            raw = _collect_social_api(query, source_cfg)
            errors.append("源 SOCIAL_API 尚未实现（离线预演）")
        elif ds == DataSource.SEARCH:
            raw = _collect_search(query, source_cfg)
            errors.append("源 SEARCH 尚未实现（离线预演）")
        else:  # CRAWLER 或未知源
            raw = []
            errors.append(f"源 {ds.value} 尚未实现（离线预演）")

        for post in raw:
            key = (post.source.value, post.external_id)
            if key in seen:
                deduped += 1
            else:
                seen.add(key)
                posts.append(post)
    except Exception as exc:  # 错误隔离：任一源失败不拖垮整体
        logger.exception("采集源 %s 失败", query.data_source.value)
        errors.append(f"采集异常：{exc}")

    if deduped:
        logger.info("去重丢弃 %d 条重复 Post", deduped)
    return CollectResult(
        posts=posts,
        fetched_at=now,
        errors=errors,
        deduped=deduped,
    )
