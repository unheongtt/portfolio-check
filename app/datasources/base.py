"""数据源基础协议与错误类型。

合规原则（统一规则）：
- 数据缺失、冲突、过期或调用失败时，一律显式返回状态（missing/stale/error），
  禁止静默生成『正常』结论，禁止用假数据兜底；
- 市场数字只来自结构化数据源，AI 层永不生成数字。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class SourceError(Exception):
    """数据源调用错误，携带结构化信息供上层呈现。"""

    def __init__(self, source: str, kind: str, message: str, code: str | None = None):
        self.source = source
        self.kind = kind          # auth / permission / timeout / network / rate_limit / schema / param
        self.message = message
        self.code = code
        super().__init__(f"[{source}] {kind}: {message}")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DataSource:
    """所有数据源实现的基础接口。"""

    name: str = "base"
    configured: bool = False

    async def health(self) -> dict[str, Any]:
        """返回数据源可用性：{configured, status, message}。"""
        return {"name": self.name, "configured": self.configured, "status": "ok" if self.configured else "disabled"}
