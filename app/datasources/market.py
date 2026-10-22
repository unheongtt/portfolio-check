"""市场数据服务：编排扶摇 / iFinD，本地缓存 + 新鲜度，为风险引擎供数。

核心合规约定：
- 每个数字都携带 source / as_of / caliber / unit / data_status；
- 缓存过期或拉取失败时显式降级标记（stale/error），绝不静默兜底；
- 数字只来自结构化数据源，AI 层永不生成数字。
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import get_settings
from ..db import get_db
from ..models import DataStatus
from .base import SourceError, utc_now_iso
from .fuyao import get_fuyao
from .ifind import get_ifind

# 缓存重拉间隔（秒）：行情 60s、历史日线 24h
QUOTE_REFETCH_SECONDS = 60
KLINE_REFETCH_SECONDS = 24 * 3600
# 相关性/波动率回看窗口（自然日）
KLINE_LOOKBACK_DAYS = 120


def _is_a_share(code: str) -> bool:
    return code.endswith((".SH", ".SZ", ".BJ"))


def _is_stale(fetched_at: str, threshold_seconds: int) -> bool:
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    return age > threshold_seconds


def _fmt_value(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


class MarketDataService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = get_db()
        self.fuyao = get_fuyao()
        self.ifind = get_ifind()

    # ------------------------------------------------------------------ 行情
    async def get_quotes(self, thscodes: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        to_fetch: list[str] = []
        now = utc_now_iso()
        cache = {r["thscode"]: r for r in self.db.query(
            "SELECT * FROM quote WHERE thscode IN (%s)" % ",".join("?" * len(thscodes)), tuple(thscodes))} if thscodes else {}
        for code in thscodes:
            row = cache.get(code)
            if row and not _is_stale(row["fetched_at"], QUOTE_REFETCH_SECONDS):
                out.append(dict(row))
            else:
                to_fetch.append(code)

        fresh: dict[str, Any] = {}
        error: SourceError | None = None
        if to_fetch:
            try:
                data = await self.fuyao.fetch_snapshot(to_fetch)
                for code, item in (data.get("items") or {}).items():
                    if not isinstance(item, dict):
                        continue
                    fresh[code] = {
                        "thscode": code, "name": item.get("name", ""),
                        "last_price": item.get("last_price"),
                        "price_change_ratio_pct": item.get("price_change_ratio_pct"),
                        "turnover": item.get("turnover"),
                        "as_of_ms": data.get("timestamp_ms"),
                        "source": "fuyao", "data_status": DataStatus.OK.value, "fetched_at": now,
                    }
            except SourceError as e:
                error = e
            except Exception as e:
                error = SourceError("fuyao", "schema", f"响应处理异常: {e}")

        for code in thscodes:
            row = fresh.get(code)
            if row:
                self.db.upsert("quote", row, ["thscode"])
                out.append(row)
                continue
            cached = cache.get(code)
            if cached:
                cached = dict(cached)
                cached["data_status"] = DataStatus.STALE.value
                cached["fetch_error"] = error.message if error else "重新拉取失败"
                out.append(cached)
                continue
            out.append({
                "thscode": code, "name": "", "last_price": None,
                "price_change_ratio_pct": None, "turnover": None, "as_of_ms": None,
                "source": "fuyao", "data_status": DataStatus.ERROR.value if error else DataStatus.MISSING.value,
                "fetch_error": error.message if error else "数据源未返回该标的",
                "fetched_at": now,
            })
        return out

    # ------------------------------------------------------------------ 估值
    async def get_valuations(self, thscodes: list[str]) -> list[dict[str, Any]]:
        a_shares = [c for c in thscodes if _is_a_share(c)]

        def _err_row(code: str, status: str, message: str) -> dict[str, Any]:
            return {"thscode": code, "name": "", "pe_ttm": None, "pb_mrq": None, "ps_ttm": None,
                    "as_of_ms": None, "data_status": status, "fetch_error": message, "source": "fuyao"}
        if not a_shares:
            return [_err_row(c, DataStatus.MISSING.value, "非 A 股标的，扶摇无估值快照") for c in thscodes]
        try:
            data = await self.fuyao.fetch_valuations(a_shares)
        except SourceError as e:
            return [_err_row(c, DataStatus.ERROR.value, e.message) for c in thscodes]
        except Exception as e:
            return [_err_row(c, DataStatus.ERROR.value, f"响应处理异常: {e}") for c in thscodes]

        now = utc_now_iso()
        out: list[dict[str, Any]] = []
        for c in thscodes:
            item = (data.get("items") or {}).get(c)
            if isinstance(item, dict):
                row = {"thscode": c, "name": item.get("name", ""), "pe_ttm": item.get("pe_ttm"),
                       "pb_mrq": item.get("pb_mrq"), "ps_ttm": item.get("ps_ttm"),
                       "as_of_ms": data.get("timestamp_ms"), "source": "fuyao",
                       "data_status": DataStatus.OK.value, "fetched_at": now}
                self.db.upsert("valuation", row, ["thscode"])
                out.append(row)
            else:
                out.append(_err_row(c, DataStatus.MISSING.value, "扶摇未返回该标的估值"))
        return out

    # ------------------------------------------------------------------ 历史收盘序列
    async def get_close_series(self, thscodes: list[str]) -> dict[str, dict[str, Any]]:
        """逐标的拉取历史日线收盘价（前复权），供相关性/波动率计算。

        返回 {thscode: {closes:[..], dates_ms:[..], data_status, source, as_of, fetch_error}}。
        """
        end_ms = int(time.time() * 1000)
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=KLINE_LOOKBACK_DAYS)).timestamp() * 1000)
        out: dict[str, dict[str, Any]] = {}
        now = utc_now_iso()

        for code in thscodes:
            if not _is_a_share(code):
                out[code] = {"closes": [], "dates_ms": [], "data_status": DataStatus.MISSING.value,
                             "source": "fuyao", "fetch_error": "非 A 股标的，扶摇无历史日线"}
                continue
            # 缓存新鲜则直接用
            cached = self.db.query(
                "SELECT date_ms, close_price, fetched_at FROM kline WHERE thscode=? ORDER BY date_ms ASC", (code,))
            if cached and cached[0]["fetched_at"] and not _is_stale(cached[0]["fetched_at"], KLINE_REFETCH_SECONDS):
                out[code] = {
                    "closes": [r["close_price"] for r in cached if r["close_price"] is not None],
                    "dates_ms": [r["date_ms"] for r in cached],
                    "data_status": DataStatus.OK.value, "source": "fuyao",
                    "as_of": cached[-1]["fetched_at"] if cached else "",
                }
                continue
            try:
                data = await self.fuyao.fetch_historical(code, start_ms, end_ms)
                items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
                if items:
                    for it in items:
                        self.db.upsert("kline", {
                            "thscode": code, "date_ms": it.get("date_ms"),
                            "close_price": it.get("close_price"), "source": "fuyao", "fetched_at": now,
                        }, ["thscode", "date_ms"])
                    out[code] = {
                        "closes": [it.get("close_price") for it in items if it.get("close_price") is not None],
                        "dates_ms": [it.get("date_ms") for it in items],
                        "data_status": DataStatus.OK.value, "source": "fuyao",
                        "as_of": now,
                    }
                else:
                    out[code] = {"closes": [], "dates_ms": [], "data_status": DataStatus.MISSING.value,
                                 "source": "fuyao", "fetch_error": "该区间无历史日线数据"}
            except SourceError as e:
                if cached:  # 过期缓存 + 失败 → stale + 旧值
                    out[code] = {
                        "closes": [r["close_price"] for r in cached if r["close_price"] is not None],
                        "dates_ms": [r["date_ms"] for r in cached],
                        "data_status": DataStatus.STALE.value, "source": "fuyao",
                        "as_of": cached[-1]["fetched_at"] if cached else "", "fetch_error": e.message,
                    }
                else:
                    out[code] = {"closes": [], "dates_ms": [], "data_status": DataStatus.ERROR.value,
                                 "source": "fuyao", "fetch_error": e.message}
        return out

    # ------------------------------------------------------------------ 健康
    async def health(self) -> dict[str, Any]:
        return {
            "sources": {
                "fuyao": await self.fuyao.health(),
                "ifind": await self.ifind.health(),
            },
            "checked_at": utc_now_iso(),
        }


_market: MarketDataService | None = None


def get_market() -> MarketDataService:
    global _market
    if _market is None:
        _market = MarketDataService()
    return _market
