"""
MCP Gateway - 二级路由聚合网关

基于 FastMCP 2.0，将多个第三方 MCP Server 聚合为少量顶层工具。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import FastMCP, Client


@dataclass
class ServerConfig:
    """上游 MCP 服务器配置"""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    
    @property
    def client_config(self) -> dict:
        """转换为 FastMCP Client 配置格式"""
        return {
            "mcpServers": {
                self.name: {
                    "command": self.command,
                    "args": self.args,
                    "env": self.env,
                }
            }
        }


class MCPGateway:
    """MCP 网关 - 聚合多个上游服务器"""
    
    def __init__(self, name: str = "MCP-Gateway"):
        self.app = FastMCP(name=name)
        self.servers: dict[str, ServerConfig] = {}
        self._tools_cache: dict[str, list[str]] = {}
    
    def load_config(self, config_path: Path | str) -> MCPGateway:
        """从 JSON 文件加载配置"""
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        for name, cfg in data.get("mcpServers", {}).items():
            self.add_server(ServerConfig(
                name=name,
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
            ))
        return self
    
    def add_server(self, server: ServerConfig) -> MCPGateway:
        """添加上游服务器并注册对应工具"""
        self.servers[server.name] = server
        self._register_tool(server)
        return self
    
    def _register_tool(self, server: ServerConfig) -> None:
        """为上游服务器注册聚合工具"""
        
        @self.app.tool(
            name=f"use_{server.name}",
            description=self._build_description(server.name),
        )
        async def dispatch(action: str, params: dict[str, Any] = {}) -> str:
            return await self._handle_dispatch(server, action, params)
    
    def _build_description(self, name: str) -> str:
        """构建工具描述"""
        return f"""与 **{name}** 子系统交互。

**参数**:
- `action`: 要调用的工具名 (使用 "list" 查看所有可用工具)
- `params`: 工具参数 (字典)

**示例**: action="read_file", params={{"path": "/tmp/test.txt"}}"""
    
    async def _handle_dispatch(
        self, 
        server: ServerConfig, 
        action: str, 
        params: dict[str, Any],
    ) -> str:
        """处理工具调用分发"""
        if action == "list":
            return await self._list_tools(server)
        return await self._call_tool(server, action, params)
    
    async def _list_tools(self, server: ServerConfig) -> str:
        """列出上游服务器的所有工具"""
        if server.name not in self._tools_cache:
            try:
                async with Client(server.client_config) as client:
                    tools = await client.list_tools()
                    self._tools_cache[server.name] = [
                        f"{t.name}: {t.description or '无描述'}" 
                        for t in tools
                    ]
            except Exception as e:
                return f"❌ 无法获取工具列表: {e}"
        
        tools = self._tools_cache[server.name]
        return f"📦 [{server.name}] 可用工具 ({len(tools)} 个):\n\n" + "\n".join(
            f"  • {t}" for t in tools
        )
    
    async def _call_tool(
        self, 
        server: ServerConfig, 
        action: str, 
        params: dict[str, Any],
    ) -> str:
        """调用上游服务器的工具"""
        try:
            async with Client(server.client_config) as client:
                result = await client.call_tool(action, params)
                return self._extract_content(result)
        except Exception as e:
            return f"❌ [{server.name}] 调用 `{action}` 失败: {e}"
    
    @staticmethod
    def _extract_content(result: Any) -> str:
        """提取调用结果内容"""
        if not hasattr(result, 'content') or not result.content:
            return str(result)
        
        parts = []
        for item in result.content:
            if hasattr(item, 'text'):
                parts.append(item.text)
            elif hasattr(item, 'data'):
                parts.append(str(item.data))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    
    def run(self) -> None:
        """运行网关服务器"""
        self.app.run()


# ============================================================
# 入口
# ============================================================

def create_gateway() -> MCPGateway:
    """创建并配置网关实例"""
    config_path = Path(__file__).parent / "config.json"
    return MCPGateway().load_config(config_path)


# 全局实例 (供 FastMCP 使用)
gateway = create_gateway()


def main() -> None:
    gateway.run()


if __name__ == "__main__":
    main()
