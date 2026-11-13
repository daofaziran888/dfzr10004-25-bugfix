"""站点配额目录：登记平台允许采集的目标站点及其硬配额。

频率策略里的日请求上限、并发数量都不能超过站点目录给出的硬配额；
目标站点必须命中目录里的站点名称（去除首尾空白后精确匹配）。
真实项目里这里会换成站点管理库/配置中心，当前用内存常量保证可复现。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SiteQuota:
    name: str
    daily_cap: int          # 站点硬配额：日请求上限不得超过该值
    max_concurrency: int    # 站点硬配额：并发数量不得超过该值
    min_interval: int       # 站点要求的最小请求间隔（毫秒）
    domain: str


SITE_CATALOG: tuple[SiteQuota, ...] = (
    SiteQuota("新闻资讯站A", daily_cap=20000, max_concurrency=8, min_interval=200, domain="news-a.example.com"),
    SiteQuota("电商商品站B", daily_cap=10000, max_concurrency=5, min_interval=500, domain="shop-b.example.com"),
    SiteQuota("论坛社区站C", daily_cap=5000, max_concurrency=3, min_interval=1000, domain="forum-c.example.com"),
    SiteQuota("政务公开站D", daily_cap=2000, max_concurrency=2, min_interval=2000, domain="gov-d.example.com"),
)

_SITE_INDEX = {site.name: site for site in SITE_CATALOG}


def normalize_site(value: object) -> str:
    """站点名称统一去首尾空白；目录匹配按归一化后的名称精确判定。"""
    return str(value or "").strip()


def get_site(name: object) -> SiteQuota | None:
    return _SITE_INDEX.get(normalize_site(name))


def site_dicts() -> list[dict[str, object]]:
    """给前端下拉框/校验提示用的只读目录。"""
    return [
        {
            "目标站点": site.name,
            "站点域名": site.domain,
            "日请求硬配额": site.daily_cap,
            "并发硬配额": site.max_concurrency,
            "最小请求间隔毫秒": site.min_interval,
        }
        for site in SITE_CATALOG
    ]
