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
    tools: list[str] = field(default_factory=list)
    
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
        # 尝试动态获取工具列表
        try:
            import asyncio
            print(f"正在连接子服务 {server.name} 以获取工具列表...")
            tools = asyncio.run(self._fetch_tools_dynamic(server))
            server.tools = tools
            print(f"成功获取 {server.name} 的 {len(tools)} 个工具")
        except Exception as e:
            print(f"⚠️ 初始化 {server.name} 失败或无法获取工具: {e}")
            # 如果获取失败，仍然注册服务，但没有工具列表提示
        
        self.servers[server.name] = server
        self._register_tool(server)
        return self

    async def _fetch_tools_dynamic(self, server: ServerConfig) -> list[str]:
        """动态获取工具列表并缓存"""
        async with Client(server.client_config) as client:
            tools = await client.list_tools()
            
            # 格式化工具描述：Name: One-line Description
            formatted_tools = []
            for t in tools:
                desc = (t.description or "无描述").strip().split('\n')[0]
                if len(desc) > 80:
                    desc = desc[:77] + "..."
                formatted_tools.append(f"{t.name}: {desc}")

            # 更新缓存，供 list 命令使用 (使用详细版)
            self._tools_cache[server.name] = [
                f"{t.name}: {t.description or '无描述'}" 
                for t in tools
            ]
            
            # 返回简要描述列表用于 Prompt
            return formatted_tools
    
    def _register_tool(self, server: ServerConfig) -> None:
        """为上游服务器注册聚合工具"""
        
        @self.app.tool(
            name=f"use_{server.name}",
            description=self._build_description(server),
        )
        async def dispatch(action: str, params: dict[str, Any] = {}) -> str:
            return await self._handle_dispatch(server, action, params)
    
    def _build_description(self, server: ServerConfig) -> str:
        """构建工具描述"""
        base_desc = f"""与 **{server.name}** 子系统交互。

**参数**:
- `action`: 要调用的工具名 (使用 "list" 查看所有可用工具)
- `params`: 工具参数 (字典)

**示例**: action="read_file", params={{"path": "/tmp/test.txt"}}"""

        if server.tools:
            # 只显示前 20 个工具，避免描述过长
            display_tools = server.tools[:30]
            tools_list = "\n".join(f"- {t}" for t in display_tools)
            more_msg = f"\n... (还有 {len(server.tools) - 30} 个工具，请使用 list 查看完整列表)" if len(server.tools) > 30 else ""
            return f"{base_desc}\n\n**可用工具列表** (部分):\n{tools_list}{more_msg}"
        
        return base_desc
    
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
        # 优先使用缓存
        if server.name in self._tools_cache:
            tools = self._tools_cache[server.name]
            return f"📦 [{server.name}] 可用工具 ({len(tools)} 个):\n\n" + "\n".join(
                f"  • {t}" for t in tools
            )

        # 缓存未命中（运行时重新获取）
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
    config_path = Path(__file__).parent / "mcps_config.json"
    return MCPGateway().load_config(config_path)


# 全局实例 (供 FastMCP 使用)
gateway = create_gateway()


def main() -> None:
    gateway.run()


if __name__ == "__main__":
    main()
