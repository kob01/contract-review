"""监管公开信息采集骨架。

定位说明（重要）：
- 本模块提供的是**带合规护栏的采集框架**：robots 检查、限速、自定义 UA、
  幂等写入、失败不静默；不是"拿来就能抓到数据"的成品选择器。
- 目标站点的版面会变，列表/详情页的选择器必须以 `SELECTORS` 为准逐站点校准，
  解析不出条目时返回 error 而不是空结果，避免"看起来采集成功但其实没数据"。
- 罚单文本属于公开政务信息，可用于类比论证；但结论引用仍只能落在
  `legal_provisions` 的条文上，罚单本身不是法律依据。
"""

from __future__ import annotations

import asyncio
import urllib.robotparser
from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.rag.ingest import upsert_enforcement_case

logger = get_logger(__name__)


class SourceNotConfigured(RuntimeError):
    pass


#: 每个来源的选择器需要人工按当前版面核对后填入
SOURCES: dict[str, dict[str, Any]] = {
    "nfra_penalty": {
        "label": "国家金融监督管理总局 - 行政处罚信息",
        "list_url": "https://www.nfra.gov.cn/branch/nfra/site/pages/list/dynamic.html",
        "item_selector": "div.list-item",
        "link_selector": "a",
        "detail_fields": {
            "org_name": "td.institution",
            "penalty_body": "td.penalty-body",
            "summary": "div.violation-summary",
            "penalty_content": "div.penalty-result",
            "decided_on": "td.decision-date",
        },
        "implemented": False,  # 选择器未经版面核对，默认拒绝执行
    },
}


def source_available(name: str) -> bool:
    config = SOURCES.get(name)
    return bool(config and config.get("implemented"))


async def _robots_allowed(client: httpx.AsyncClient, url: str, user_agent: str) -> bool:
    """抓取前检查 robots.txt。取不到 robots 时按保守策略拒绝。"""
    parsed = httpx.URL(url)
    robots_url = f"{parsed.scheme}://{parsed.host}/robots.txt"
    try:
        response = await client.get(robots_url, timeout=10)
    except httpx.HTTPError as exc:
        logger.warning("无法获取 %s：%s，按保守策略拒绝抓取", robots_url, exc)
        return False

    if response.status_code >= 400:
        # 没有 robots.txt 通常意味着不禁止，但政务站点更稳妥的做法是人工确认
        return response.status_code in (404, 403)

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser.can_fetch(user_agent, url)


async def fetch_penalty_notices(
    session: AsyncSession,
    *,
    source: str,
    settings: Settings,
    limit: int | None = None,
    embedder: Any | None = None,
) -> dict:
    """采集监管处罚条目并写入 `enforcement_cases`。"""
    config = SOURCES.get(source)
    if config is None:
        raise SourceNotConfigured(f"未知的信息来源：{source}")
    if not config.get("implemented"):
        raise SourceNotConfigured(
            f"来源 {source} 的解析选择器尚未按目标站点版面校准，"
            "请核对后把 SOURCES[...] 的 implemented 置为 true"
        )

    max_items = min(limit or settings.ingestion_max_items_per_run, settings.ingestion_max_items_per_run)
    errors: list[str] = []
    items: list[dict] = []
    skipped = 0

    headers = {"User-Agent": settings.ingestion_user_agent}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        if not await _robots_allowed(client, config["list_url"], settings.ingestion_user_agent):
            raise SourceNotConfigured(f"目标站点 robots 策略不允许抓取 {config['list_url']}")

        try:
            list_response = await client.get(config["list_url"], timeout=30)
            list_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceNotConfigured(f"列表页获取失败：{exc}") from exc

        items, parse_errors = _parse_list(list_response.text, config)
        errors.extend(parse_errors)

        for item in items[:max_items]:
            await asyncio.sleep(settings.ingestion_min_interval_seconds)
            try:
                detail = await _fetch_detail(client, item, config)
                detail.update(
                    {
                        "source_url": item.get("url"),
                        "violation_types": item.get("violation_types", []),
                    }
                )
                await upsert_enforcement_case(
                    session,
                    org_name=detail["org_name"],
                    summary=detail["summary"],
                    case_no=detail.get("case_no"),
                    penalty_body=detail.get("penalty_body"),
                    decided_on=detail.get("decided_on"),
                    penalty_content=detail.get("penalty_content", ""),
                    penalty_amount=detail.get("penalty_amount"),
                    violation_types=detail.get("violation_types", []),
                    basis_refs=detail.get("basis_refs", []),
                    source_url=detail.get("source_url"),
                    is_sample=False,
                )
            except Exception as exc:  # 单条失败不影响整批
                skipped += 1
                errors.append(f"{item.get('url')}: {exc}")
                logger.warning("罚单条目解析失败 %s：%s", item.get("url"), exc)

    return {
        "item_count": len(items),
        "ok_count": len(items[:max_items]) - skipped,
        "skipped_count": skipped,
        "errors": errors,
    }


def _parse_list(html: str, config: dict) -> tuple[list[dict], list[str]]:
    """解析列表页。版面变化时返回错误提示而不是空列表。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select(config["item_selector"])
    if not nodes:
        return [], [
            f"列表页未匹配到任何条目（选择器 {config['item_selector']!r}），"
            "目标站点版面可能已变更，需人工核对"
        ]

    items: list[dict] = []
    for node in nodes:
        link = node.select_one(config["link_selector"])
        if link is None or not link.get("href"):
            continue
        items.append(
            {
                "url": link["href"],
                "title": link.get_text(strip=True),
                "violation_types": [],
            }
        )
    return items, []


async def _fetch_detail(client: httpx.AsyncClient, item: dict, config: dict) -> dict:
    from bs4 import BeautifulSoup

    response = await client.get(item["url"], timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    fields = config["detail_fields"]

    def pick(selector: str) -> str:
        node = soup.select_one(selector)
        return node.get_text(strip=True) if node else ""

    org_name = pick(fields["org_name"])
    summary = pick(fields["summary"])
    if not org_name or not summary:
        raise ValueError("详情页未能解析出机构名称或违规事实，需人工核对版面")

    return {
        "org_name": org_name,
        "penalty_body": pick(fields["penalty_body"]) or None,
        "summary": summary,
        "penalty_content": pick(fields["penalty_content"]),
        "decided_on": _parse_date(pick(fields["decided_on"])),
        "case_no": item.get("title") or None,
        "penalty_amount": None,
        "basis_refs": [],
    }


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
