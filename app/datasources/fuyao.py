"""扶摇（同花顺金融数据 API）REST 数据源。

契约来源：https://fuyao.aicubes.cn/llms-full.txt（接口契约以该文档为准）
- Base URL: https://fuyao.aicubes.cn，鉴权头 X-api-key
- 统一响应信封 {code, message, request_id, data}，code=0 成功；HTTP 恒 200，限流可 429
- 错误码：2001 无/失效 Key；2003 无权限；1001/1003/1004 参数错误
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import get_settings
from .base import DataSource, SourceError

CODE_MESSAGES = {
    2001: "API Key 缺失或无效",
    2003: "API Key 无该 capability 权限",
    1001: "缺少必需参数",
    1003: "参数越界",
    1004: "参数组合不合法",
}


class FuyaoDataSource(DataSource):
    name = "fuyao"

    def __init__(self) -> None:
        s = get_settings()
        self.base_url = s.fuyao_base_url.rstrip("/")
        self.api_key = s.fuyao_api_key
        self.configured = bool(self.api_key)
        self.timeout = 15.0

    def _headers(self) -> dict[str, str]:
        return {"X-api-key": self.api_key}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise SourceError(self.name, "auth", "未配置 FUYAO_API_KEY，扶摇数据源不可用")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}{path}", params=params, headers=self._headers())
        except httpx.TimeoutException as e:
            raise SourceError(self.name, "timeout", f"请求超时（>{self.timeout}s）: {path}") from e
        except httpx.HTTPError as e:
            raise SourceError(self.name, "network", f"网络错误: {e}") from e

        if resp.status_code == 429:
            raise SourceError(self.name, "rate_limit", "触发限流（HTTP 429），请稍后重试或降低并发")
        if resp.status_code >= 400:
            raise SourceError(self.name, "http", f"HTTP {resp.status_code}", code=str(resp.status_code))
        try:
            payload = resp.json()
        except ValueError as e:
            raise SourceError(self.name, "schema", "响应不是合法 JSON（上游格式变化）") from e
        code = payload.get("code")
        if code != 0:
            raise SourceError(
                self.name, "business",
                CODE_MESSAGES.get(code, f"业务错误 code={code}") + (f"（message={payload.get('message')}）" if payload.get("message") else ""),
                code=str(code),
            )
        return payload.get("data") or {}

    # ---- 接口封装 ---------------------------------------------------------

    @staticmethod
    def _items_by_thscode(data: dict[str, Any]) -> dict[str, Any]:
        """把 item 列表按 thscode 建索引；上游结构异常时显式报 schema 错误。"""
        raw = data.get("item")
        if raw is None:
            return {}
        if not isinstance(raw, list):
            raise SourceError("fuyao", "schema", "响应 item 字段不是列表（上游格式变化）")
        out: dict[str, Any] = {}
        for it in raw:
            if not isinstance(it, dict) or not it.get("thscode"):
                continue
            out[it["thscode"]] = it
        return out

    async def fetch_snapshot(self, thscodes: list[str]) -> dict[str, Any]:
        """批量行情快照。thscode 格式如 600519.SH。"""
        data = await self._get("/api/a-share/prices/snapshot", {"thscodes": ",".join(thscodes)})
        return {"timestamp_ms": data.get("timestamp"), "items": self._items_by_thscode(data)}

    async def fetch_historical(self, thscode: str, start_ms: int, end_ms: int,
                               interval: str = "1d", adjust: str = "forward") -> dict[str, Any]:
        """单只标的 A 股历史日线（前复权），用于相关性/波动率计算。

        接口强约束：每次请求仅一个 thscode，[start, end] 跨度 ≤10 年。
        """
        data = await self._get("/api/a-share/prices/historical", {
            "thscode": thscode, "interval": interval,
            "start": str(start_ms), "end": str(end_ms), "adjust": adjust})
        return {"timestamp_ms": data.get("timestamp"), "items": data.get("item") or []}

    async def fetch_income_statements(self, thscode: str, period: str = "annual", limit: int = 4) -> dict[str, Any]:
        data = await self._get("/api/a-share/financials/income-statements",
                               {"thscode": thscode, "period": period, "limit": str(limit)})
        return {"timestamp_ms": data.get("timestamp"), "items": data.get("item") or []}

    async def fetch_balance_sheets(self, thscode: str, period: str = "annual", limit: int = 4) -> dict[str, Any]:
        data = await self._get("/api/a-share/financials/balance-sheets",
                               {"thscode": thscode, "period": period, "limit": str(limit)})
        return {"timestamp_ms": data.get("timestamp"), "items": data.get("item") or []}

    async def fetch_cash_flow_statements(self, thscode: str, period: str = "annual", limit: int = 4) -> dict[str, Any]:
        data = await self._get("/api/a-share/financials/cash-flow-statements",
                               {"thscode": thscode, "period": period, "limit": str(limit)})
        return {"timestamp_ms": data.get("timestamp"), "items": data.get("item") or []}

    async def fetch_indicators(self, thscode: str, report: str) -> dict[str, Any]:
        """财务指标（growth/profitability/solvency/operation/cash-flow），report 如 2024-4。"""
        data = await self._get("/api/a-share/financials/indicators", {"thscode": thscode, "report": report})
        return {"report": data.get("report"), "abilities": data.get("abilities") or []}

    async def fetch_valuations(self, thscodes: list[str]) -> dict[str, Any]:
        data = await self._get("/api/a-share/valuations/snapshot", {"thscodes": ",".join(thscodes)})
        return {"timestamp_ms": data.get("timestamp"), "items": self._items_by_thscode(data)}

    async def fetch_concept_catalog(self, tag: str = "cn_concept") -> list[dict[str, Any]]:
        data = await self._get("/api/a-share-index/catalog/ths-index-list", {"tag": tag})
        return data.get("item") or []

    async def fetch_index_constituents(self, thscode: str) -> list[dict[str, Any]]:
        data = await self._get("/api/a-share-index/constituents/ths-stock-list", {"thscode": thscode})
        return data.get("item") or []

    async def health(self) -> dict[str, Any]:
        base: dict[str, Any] = {"name": self.name, "configured": self.configured}
        if not self.configured:
            base["status"] = "disabled"
            base["message"] = "未配置扶摇 API Key（见 .env.example）"
            return base
        try:
            # 用一次最小请求验证 Key（概念指数目录，轻量）
            data = await self._get("/api/a-share-index/catalog/ths-index-list", {"tag": "cn_concept"})
            base["status"] = "ok"
            base["message"] = f"已连接，概念指数目录 {len(data.get('item') or [])} 条"
        except SourceError as e:
            base["status"] = "error"
            base["message"] = f"{e.kind}: {e.message}"
        return base


_fuyao: FuyaoDataSource | None = None


def get_fuyao() -> FuyaoDataSource:
    global _fuyao
    if _fuyao is None:
        _fuyao = FuyaoDataSource()
    return _fuyao
