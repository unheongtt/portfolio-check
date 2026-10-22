"""REST API：持仓体检主链路 + 数据源健康 + AI 产出追溯。"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..ai.analyzer import get_engine
from ..db import get_db
from ..datasources.market import get_market
from ..models import DataStatus, Holding, PortfolioInput, now_iso
from ..risk.engine import analyze

router = APIRouter(prefix="/api")


def _db() -> Any:
    return get_db()


def _membership_map() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in _db().query("SELECT * FROM industry_concept"):
        try:
            concepts = json.loads(r["concepts"] or "[]")
        except ValueError:
            concepts = []
        out[r["thscode"]] = {
            "industry": r["industry"], "industry_source": r["industry_source"],
            "concepts": concepts,
        }
    return out


def _to_dict_map(rows: list[dict[str, Any]], key: str = "thscode") -> dict[str, dict[str, Any]]:
    return {r[key]: r for r in rows}


# ------------------------------------------------------------------ 示例组合
@router.get("/sample-portfolio")
def get_sample_portfolio() -> dict[str, Any]:
    db = _db()
    holdings = db.query("SELECT * FROM sample_holding WHERE portfolio_id='sample' ORDER BY ord")
    return {
        "id": "sample",
        "name": "示例组合（仅供演示，非投资建议）",
        "holdings": [{"thscode": h["thscode"], "name": h["name"], "market": h["market"],
                      "weight_pct": h["weight_pct"]} for h in holdings],
    }


# ------------------------------------------------------------------ 体检主链路
class CheckRequest(BaseModel):
    name: str = "我的组合"
    holdings: list[Holding] = Field(min_length=1, max_length=50)


@router.post("/check")
async def check(req: CheckRequest) -> dict[str, Any]:
    """执行持仓体检：拉数 → 风险计算 → AI 解读 → 报告。"""
    holdings = [h.model_dump() for h in req.holdings]
    total_w = sum(h["weight_pct"] for h in holdings)
    if abs(total_w - 100.0) > 1.0:
        raise HTTPException(400, f"持仓权重之和应为 100%（当前 {total_w:g}%）")

    thscodes = [h["thscode"] for h in holdings]
    membership = _membership_map()
    market = get_market()

    # 并行拉取：行情（展示快照）、估值、历史日线（相关性/波动）
    quotes_l, valuations_l, close_series = await asyncio.gather(
        market.get_quotes(thscodes),
        market.get_valuations(thscodes),
        market.get_close_series(thscodes),
    )
    quotes = _to_dict_map(quotes_l)
    valuations = _to_dict_map(valuations_l)

    # 用行情补齐中文名（快照接口无中文名，公司名从示例/种子匹配不到时留空）
    for h in holdings:
        q = quotes.get(h["thscode"]) or {}
        if q.get("last_price") is not None and not h.get("name"):
            h["name"] = q.get("name") or h["thscode"]

    findings = analyze(holdings, membership, valuations, close_series)

    # AI 解读（每项 + 整体），全部落库待复核
    engine = get_engine()
    narratives = await asyncio.gather(*(engine.narrate_finding(f) for f in findings))
    overall = await engine.summarize(findings, req.name)

    report_id = f"rpt-{uuid.uuid4().hex[:12]}"
    report = {
        "id": report_id,
        "portfolio": {"name": req.name, "holdings": holdings},
        "generated_at": now_iso(),
        "data_sources": await market.health(),
        "holdings_snapshot": [
            {
                "thscode": h["thscode"], "name": h.get("name") or h["thscode"],
                "weight_pct": h["weight_pct"],
                "last_price": quotes.get(h["thscode"], {}).get("last_price"),
                "price_change_ratio_pct": quotes.get(h["thscode"], {}).get("price_change_ratio_pct"),
                "data_status": quotes.get(h["thscode"], {}).get("data_status", DataStatus.MISSING.value),
                "fetch_error": quotes.get(h["thscode"], {}).get("fetch_error", ""),
            } for h in holdings
        ],
        "findings": [
            {**f, "narrative": narratives[i]} for i, f in enumerate(findings)
        ],
        "overall": overall,
    }
    _db().upsert("report", {
        "id": report_id, "portfolio_name": req.name,
        "payload": json.dumps(report, ensure_ascii=False), "created_at": now_iso(),
    }, ["id"])
    return report


@router.get("/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    row = _db().query_one("SELECT * FROM report WHERE id=?", (report_id,))
    if not row:
        raise HTTPException(404, "报告不存在")
    return json.loads(row["payload"])


# ------------------------------------------------------------------ AI 产出追溯
@router.get("/ai-outputs")
def get_ai_outputs() -> dict[str, Any]:
    rows = _db().query("SELECT * FROM ai_output ORDER BY created_at DESC LIMIT 100")
    return {"outputs": [{**r, "input_refs": json.loads(r["input_refs"] or "[]"),
                         "output": json.loads(r["output"] or "{}")} for r in rows]}


class ReviewRequest(BaseModel):
    review_status: str = "reviewed"


@router.post("/ai-outputs/{output_id}/review")
def review_ai_output(output_id: str, req: ReviewRequest) -> dict[str, Any]:
    db = _db()
    row = db.query_one("SELECT * FROM ai_output WHERE id=?", (output_id,))
    if not row:
        raise HTTPException(404, "AI 产出不存在")
    if req.review_status not in ("pending", "reviewed"):
        raise HTTPException(400, "review_status 只能为 pending/reviewed")
    db.execute("UPDATE ai_output SET review_status=? WHERE id=?", (req.review_status, output_id))
    return {"id": output_id, "review_status": req.review_status}


# ------------------------------------------------------------------ 健康
@router.get("/health")
async def health() -> dict[str, Any]:
    return await get_market().health()
