"""
麦当劳 MCP Server 客户端
接入方式：Streamable HTTP + Authorization: Bearer <token>

设计要点
--------
1. Token 只从环境变量 MCD_MCP_TOKEN 读取，绝不落盘、不进日志
2. 保持会话（Streamable HTTP 会返回 Mcp-Session-Id）
3. 限流保护：每分钟 600 次是官方上限，这里保守取 480，留出余量
4. 只读工具与写操作工具在代码层面区分，避免误调用
"""
import json
import os
import time
import urllib.error
import urllib.request

MCP_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2024-11-05"

# 官方文档标注的写操作工具。除用户明确确认外，一律不调用。
WRITE_TOOLS = frozenset({
    "create-order",
    "cancel-order",
    "auto-bind-coupons",
    "draw-lottery",
    "mall-create-order",
    "party-order-create",
    "delivery-create-address",
})

# 仅本项目会用到的只读工具
READONLY_TOOLS = frozenset({
    "now-time-info",
    "mall-points-products",
    "mall-product-detail",
    "query-party-city",
    "query-party-store",
    "query-party-store-date",
    "query-party-store-session",
    "list-nutrition-foods",
})


class MCPError(RuntimeError):
    """MCP 调用异常。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class McdClient:
    """麦当劳 MCP Server 轻客户端。零第三方依赖，仅用标准库。"""

    def __init__(self, token=None, min_interval=0.125):
        self.token = (token or os.environ.get("MCD_MCP_TOKEN", "")).strip()
        if not self.token:
            raise MCPError(
                "未找到 MCP Token。请在 open.mcd.cn 控制台申请，"
                "然后设置环境变量：export MCD_MCP_TOKEN=你的token"
            )
        self._session_id = None
        self._last_call = 0.0
        # 600 次/分钟上限，保守取 480/分钟，即两次调用间隔 >= 0.125s
        self._min_interval = min_interval

    # ---------- 底层协议 ----------

    def _throttle(self):
        gap = time.time() - self._last_call
        if gap < self._min_interval:
            time.sleep(self._min_interval - gap)
        self._last_call = time.time()

    def _rpc(self, method, params=None, is_notification=False):
        self._throttle()
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not is_notification:
            body["id"] = 1

        req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode("utf-8"))
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("MCP-Protocol-Version", PROTOCOL_VERSION)

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            code = e.code
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if code == 401:
                raise MCPError(
                    "MCP Token 无效或已过期（401）。请到 open.mcd.cn 控制台重新申请。", 401
                ) from e
            if code == 429:
                raise MCPError("触发限流（429，600 次/分钟），请稍后重试。", 429) from e
            raise MCPError(f"MCP 请求失败（HTTP {code}）：{detail}", code) from e
        except urllib.error.URLError as e:
            raise MCPError(f"网络异常，无法连接 mcp.mcd.cn：{e.reason}") from e

        if not raw.strip():
            return None

        # 兼容 SSE 包装格式
        if raw.lstrip().startswith(("event:", "data:")):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        return json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            return None

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise MCPError(f"MCP 返回非 JSON 内容：{raw[:200]}") from e

    def _unwrap(self, resp):
        if not isinstance(resp, dict):
            return resp
        if "error" in resp:
            err = resp["error"]
            raise MCPError(f"MCP 返回错误：{err.get('message', err)}")
        return resp.get("result", resp)

    # ---------- 连接 ----------

    def connect(self):
        """握手，建立会话。"""
        result = self._unwrap(self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-kids-calendar", "version": "1.0"},
        }))
        self._rpc("notifications/initialized", {}, is_notification=True)
        return result

    def list_tools(self):
        """获取服务端实际暴露的工具清单。"""
        self._unwrap(self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-kids-calendar", "version": "1.0"},
        }))
        self._rpc("notifications/initialized", {}, is_notification=True)
        return self._unwrap(self._rpc("tools/list", {})).get("tools", [])

    # ---------- 业务调用 ----------

    def call_tool(self, name, arguments=None, allow_write=False):
        """
        调用一个工具。

        参数
        ----
        name : 工具名
        arguments : 入参
        allow_write : 是否允许调用写操作工具。
                      默认 False。调用写操作必须显式传 True，
                      调用方需自行完成用户确认流程。
        """
        if name in WRITE_TOOLS and not allow_write:
            raise MCPError(
                f"拒绝调用写操作工具 {name}。如确需调用，请显式传入 allow_write=True。"
            )
        result = self._unwrap(self._rpc("tools/call", {
            "name": name,
            "arguments": arguments or {},
        }))
        return result


def get_client():
    """便捷入口：读取环境变量创建客户端。"""
    return McdClient()