# MCP Gateway - 二级路由聚合网关

一个 MCP 网关，将多个第三方 MCP Server 聚合为少量顶层工具，大幅减少 LLM 的上下文占用与解决某些IDE没法支持太多tools的问题。

## 效果图
![网关mcp示例图](assets/image.png)

## 工作原理

```
┌─────────────────────────────────────────────────────┐
│                   MCP Gateway                       │
├─────────────────────────────────────────────────────┤
│ 对外暴露的工具 (仅 2 个):                           │
│   - use_git(action, params)                         │
│   - use_filesystem(action, params)                  │
├─────────────────────────────────────────────────────┤
│ 内部连接:                                           │
│   ├── @modelcontextprotocol/server-filesystem      │
│   └── mcp-server-git                               │
└─────────────────────────────────────────────────────┘
```

**效果**：无论后台有多少个 MCP Server，LLM 只看到 N 个工具（N = Server 数量）。

## 安装

```bash
# 克隆项目
git clone https://github.com/你的用户名/reverse-mcps.git
cd reverse-mcps/mcp_gateway

pip install fastmcp
```

## 配置

编辑 `config.json`，添加您原来的 MCP Server：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "【你的python.exe路径】",
      "args": ["【你的py文件路径】"]
    },
    "git": {
      "command": "【你的python.exe路径】",
      "args": ["【你的py文件路径】"]
    },
    "github": {
      "command": "【你的python.exe路径】",
      "args": ["【你的py文件路径】"]
    }
  }
}
```

去IDE中编辑Gateway-Mcp的配置文件，我的参考如下
> 理论上autoApprove不需要加，但是为了解决某些ide不能自动调用工具的问题，我加了一下，你的估计不需要
```json
{
  "mcpServers": {
    "GateWay-Mcp": {
      "command": "C:\\Users\\xiaofeng\\.pyenv\\pyenv-win\\versions\\3.11.9\\python.exe",
      "args": [
        "C:\\Users\\xiaofeng\\Desktop\\projects\\Gateway-Mcp\\gateway_mcp_server.py"
      ],
      "disabled": false,
      "autoApprove": [
        "use_ida-pro-mcp",
        "use_jadx-mcp-server",
        "use_adb-mcp",
        "use_proxypin-mcp"
      ]
    }
  }
}
```

## 使用示例

LLM 调用方式：

```python
# 列出 filesystem 的所有可用工具
use_filesystem(action="list", params={})

# 读取文件
use_filesystem(action="read_file", params={"path": "/tmp/test.txt"})

# 查看 git 状态
use_git(action="git_status", params={})

# 提交代码
use_git(action="git_commit", params={"message": "fix: bug"})
```

## 添加更多 MCP Server

只需在 `mcps_config.json` 中添加新的条目，Gateway 会自动为其创建对应的 `use_{name}` 工具和拉取工具最新描述。

## 许可证

MIT
