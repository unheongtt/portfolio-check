"""iFinD MCP 数据源：最小 streamable HTTP MCP 客户端。

契约来源：iFinD MCP 官方 skill（ifind-finance-data）与门户 https://mcp.51ifind.com
- JSON-RPC 2.0 over HTTPS；鉴权头 Authorization: <token>
- 会话：initialize 返回 Mcp-Session-Id，后续请求携带
- 服务按类别分端点：stock / fund / edb / news / bond / global_stock / index / future

工具名随用户权益与版本变化：list_tools() 动态探测；具体取数在 MarketDataService
中按映射表调用，映射不到的字段显式返回 missing（不静默兜底）。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..config import get_settings
from .base import DataSource, SourceError

# 端点路径与 server_type 名称不一致（global_stock → global-stock），显式映射避免 404
SERVER_ENDPOINTS = {
    "stock": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-stock-mcp",
    "fund": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-fund-mcp",
    "edb": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-edb-mcp",
    "news": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-news-mcp",
    "bond": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-bond-mcp",
    "global_stock": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-global-stock-mcp",
    "index": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-index-mcp",
    "future": "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-futures-mcp",
}
SERVER_TYPES = tuple(SERVER_ENDPOINTS.keys())


class IfindMcpClient:
    """单个 server_type 的 MCP 会话客户端。"""

    def __init__(self, server_type: str, url: str, token: str, timeout: float = 30.0):
        if server_type not in SERVER_TYPES:
            raise ValueError(f"unknown server_type: {server_type}")
        self.server_type = server_type
        self.url = url
        self.token = token
        self.timeout = timeout
        self._session_id: str | None = None
        self._tools: list[dict[str, Any]] | None = None
        self._req_id = 0
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, server_type: str) -> "IfindMcpClient":
        s = get_settings()
        url = s.ifind_mcp_url or SERVER_ENDPOINTS[server_type]
        return cls(server_type, url, s.ifind_mcp_token)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _payload(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._req_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
        if params is not None:
            msg["params"] = params
        return msg

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": self.token,
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _post(self, payload: dict[str, Any]) -> tuple[httpx.Response, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload, headers=self._headers())
        except httpx.TimeoutException as e:
            raise SourceError("ifind", "timeout", f"请求超时（>{self.timeout}s）") from e
        except httpx.HTTPError as e:
            raise SourceError("ifind", "network", f"网络错误: {e}") from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp, data

    async def initialize(self) -> None:
        async with self._lock:
            if self._session_id:
                return
            if not self.configured:
                raise SourceError("ifind", "auth", "未配置 IFIND_MCP_TOKEN")
            session: str | None = None
            for attempt in (1, 2):  # 上游偶发不返回 session 头：重试一次再报错
                resp, _data = await self._post(self._payload(
                    "initialize",
                    {"protocolVersion": "2025-03-26", "capabilities": {},
                     "clientInfo": {"name": "chainlens", "version": "0.1.0"}},
                ))
                if resp.status_code == 401:
                    raise SourceError("ifind", "auth", "token 无效（HTTP 401）")
                if resp.status_code == 403:
                    raise SourceError("ifind", "permission", "token 权限不足（HTTP 403）")
                session = resp.headers.get("mcp-session-id")
                if session:
                    break
            if not session:
                raise SourceError("ifind", "schema", "initialize 未返回 Mcp-Session-Id（重试后仍缺失，上游协议变化）")
            self._session_id = session
            await self._post(self._payload("notifications/initialized"))

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            await self.initialize()
            resp, data = await self._post(self._payload("tools/list", {}))
            if resp.status_code >= 400:
                raise SourceError("ifind", "http", f"tools/list HTTP {resp.status_code}", code=str(resp.status_code))
            tools = (data.get("result") or {}).get("tools") or []
            self._tools = tools
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        await self.initialize()
        resp, data = await self._post(self._payload("tools/call", {"name": tool_name, "arguments": arguments}))
        if "error" in data:
            err = data["error"]
            raise SourceError("ifind", "business",
                              f"工具 {tool_name} 返回错误: {err.get('message', err)}",
                              code=str(err.get("code", "")))
        if resp.status_code >= 400:
            raise SourceError("ifind", "http", f"tools/call HTTP {resp.status_code}", code=str(resp.status_code))
        result = (data.get("result") or {})
        # MCP 结果可能是 {content:[{type:text,text:...}]} 结构
        return result


class IfindDataSource(DataSource):
    """门面：按 server_type 惰性创建客户端，供 MarketDataService 使用。"""

    name = "ifind"

    def __init__(self) -> None:
        s = get_settings()
        self.configured = bool(s.ifind_mcp_token)
        self._clients: dict[str, IfindMcpClient] = {}

    def client(self, server_type: str) -> IfindMcpClient:
        if server_type not in self._clients:
            self._clients[server_type] = IfindMcpClient.from_settings(server_type)
        return self._clients[server_type]

    async def health(self) -> dict[str, Any]:
        base: dict[str, Any] = {"name": self.name, "configured": self.configured}
        if not self.configured:
            base["status"] = "disabled"
            base["message"] = "未配置 iFinD MCP Token（见 .env.example）"
            return base
        try:
            tools = await self.client("stock").list_tools()
            base["status"] = "ok"
            base["message"] = f"已连接，stock 服务可用工具 {len(tools)} 个"
        except SourceError as e:
            base["status"] = "error"
            base["message"] = f"{e.kind}: {e.message}"
        return base


_ifind: IfindDataSource | None = None


def get_ifind() -> IfindDataSource:
    global _ifind
    if _ifind is None:
        _ifind = IfindDataSource()
    return _ifind
