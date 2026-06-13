# MCP Gateway - 低 token 二级路由聚合网关

一个 MCP 网关，把多个第三方 MCP Server 聚合成少量顶层工具，适合“10 个 server × 每个 20 个 tool”这种场景：客户端只看到网关工具，真实工具通过 `search -> describe -> call` 按需发现和调用。

## 设计目标

- **减少上下文占用**：不把 200 个上游工具 schema 一次性塞给模型。
- **减少误填参数**：搜索结果直接暴露 `required` 和 `arguments_template`，调用前按上游 `inputSchema.required` 做预校验。
- **按域过滤**：每个 server 可配置 `profiles`，例如 `jadx`、`ida`、`android`、`native`，搜索时只扫需要的域。
- **故障隔离**：某个上游离线时不影响其他上游；按工具名前缀调用时只连接对应 server。
- **真实输出不截断**：省 token 靠工具发现阶段压缩；`call_gateway_tool` 默认完整返回上游结果。
- **兼容旧模式**：保留 `legacy` 的 `use_<server>(action, params)`，但推荐 `search` 模式。

## 工作原理

### search 模式（推荐）

```
┌─────────────────────────────────────────────────────┐
│                   MCP Gateway                       │
├─────────────────────────────────────────────────────┤
│ 对外暴露的工具:                                     │
│   - gateway_status                                  │
│   - search_gateway_tools(query, profile?, server?)  │
│   - describe_gateway_tool(name)                     │
│   - call_gateway_tool(name, arguments)              │
├─────────────────────────────────────────────────────┤
│ 内部实现:                                           │
│   ├── 按 server/profile 懒加载上游 tools             │
│   ├── 本地轻量检索 + 短 TTL 索引缓存                 │
│   ├── 返回紧凑摘要，不展开完整 schema                │
│   ├── describe 时才返回单个工具完整 input schema     │
│   └── call 前校验 required / unexpected 参数         │
└─────────────────────────────────────────────────────┘
```

推荐调用顺序：

1. `gateway_status(profile="jadx")`：确认对应上游是否在线。
2. `search_gateway_tools(query="class source", profile="jadx")`：搜真实工具，只返回紧凑摘要。
3. `describe_gateway_tool(name="jadx_mcp_server_get_class_source")`：必要时查看完整 schema。
4. `call_gateway_tool(name="...", arguments={...})`：用真实参数字典调用。

### legacy 模式（兼容）

```
use_filesystem(action="list", params={})
use_git(action="git_status", params={})
```

这种模式顶层工具数 = server 数，且 `action/params` 容易被模型填错；仅建议兼容旧客户端时使用。

## 安装

```bash
git clone https://github.com/LunFengChen/Gateway-Mcp.git
cd Gateway-Mcp
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 配置

编辑 `mcps_config.json`：

```json
{
  "gateway": {
    "mode": "search",
    "search_tool_name": "search_gateway_tools",
    "call_tool_name": "call_gateway_tool",
    "describe_tool_name": "describe_gateway_tool",
    "max_results": 8,
    "always_visible": ["gateway_status"],
    "probe_timeout_sec": 3.0,
    "status_preview_count": 3,
    "error_max_chars": 280,
    "search_optional_fields_limit": 6,
    "search_description_max_chars": 96,
    "legacy_list_max_items": 24,
    "call_result_max_chars": 0,
    "tool_index_ttl_sec": 30.0
  },
  "mcpServers": {
    "ida-pro-mcp": {
      "command": "/path/to/ida-python/bin/ida-pro-mcp",
      "args": [],
      "profiles": ["reverse", "native", "ida"],
      "disabled": false
    },
    "jadx-mcp-server": {
      "command": "/path/to/jadx-mcp-server/.venv/bin/python",
      "args": ["/path/to/jadx_mcp_server.py", "--jadx-host", "127.0.0.1", "--jadx-port", "8650"],
      "profiles": ["reverse", "android", "jadx"],
      "disabled": false
    },
    "binary-ninja-mcp": {
      "command": "/path/to/binary_ninja_mcp/.venv/bin/python",
      "args": ["/path/to/binary_ninja_mcp/bridge/binja_mcp_bridge.py"],
      "profiles": ["reverse", "native", "binaryninja", "bn"],
      "disabled": false
    }
  }
}
```

在 IDE / Agent 中只配置 Gateway：

```json
{
  "mcpServers": {
    "GateWay-Mcp": {
      "command": "/path/to/Gateway-Mcp/.venv/bin/python",
      "args": [
        "/path/to/Gateway-Mcp/gateway_mcp_server.py",
        "--config",
        "/path/to/Gateway-Mcp/mcps_config.json"
      ],
      "disabled": false
    }
  }
}
```

## 使用示例

```python
# 查看状态，可用 profile/server 缩小范围
gateway_status(profile="jadx")

# 搜索工具：结果包含 required 和 arguments_template
search_gateway_tools(query="class source", profile="jadx")

# 示例搜索结果片段：
# {
#   "name": "jadx_mcp_server_get_class_source",
#   "required": ["class_name"],
#   "arguments_template": {"class_name": "<required>"}
# }

# 查看单个工具完整 input schema
describe_gateway_tool(name="jadx_mcp_server_get_class_source")

# 调用真实工具：arguments 必须是上游工具真实参数字典
call_gateway_tool(
    name="jadx_mcp_server_get_class_source",
    arguments={"class_name": "com.demo.MainActivity"}
)
```

## 添加更多 MCP Server

只需在 `mcps_config.json` 的 `mcpServers` 里新增条目，并给它打上合适的 `profiles`：

- `profiles`: 用来缩小搜索/状态探测范围，避免一次扫所有 server。
- `disabled`: 临时关闭某个上游，不影响其他上游。
- `call_result_max_chars`: `0` 表示真实工具输出不截断；只有你显式传 `max_chars` 才会限制单次调用输出。
- 工具名前缀：`ida-pro-mcp` 会变成 `ida_pro_mcp_`，`jadx-mcp-server` 会变成 `jadx_mcp_server_`。

## 设计依据

当前实现采用“工具摘要 + 按需 schema + 统一调用”的 progressive disclosure 思路：

- MCP 官方 tools 协议本身支持 `tools/list` 返回 `inputSchema`，以及 `tools/call` 调用工具；网关在这两步之间做索引和裁剪。
- FastMCP 支持 proxy / transform 类网关思路，但直接代理大量工具仍会把工具面暴露给客户端；本项目选择自定义低 token API。
- 同类网关/平台常见做法也是把大量工具改成搜索或 catalog，再按需展开 schema。

## 许可证

MIT
