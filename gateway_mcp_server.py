"""
MCP Gateway

支持两种模式：

1. search（推荐）
   自定义低 token 工具索引：search -> describe -> call。
   顶层只暴露少量网关工具：
   - gateway_status
   - search_gateway_tools
   - describe_gateway_tool
   - call_gateway_tool
   上游工具不会直接铺满到客户端上下文里。

2. legacy
   兼容旧项目的一层包装风格：
   - use_<server>(action, params)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass(slots=True)
class ServerConfig:
    """单个上游 MCP Server 配置"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    profiles: list[str] = field(default_factory=list)
    disabled: bool = False

    @property
    def client_config(self) -> dict[str, Any]:
        return {
            "mcpServers": {
                self.name: {
                    "command": self.command,
                    "args": self.args,
                    "env": self.env,
                }
            }
        }

    @property
    def safe_name(self) -> str:
        name = re.sub(r"[^0-9A-Za-z]+", "_", self.name).strip("_").lower()
        if not name:
            name = "server"
        if name[0].isdigit():
            name = f"s_{name}"
        return name

    @property
    def tool_prefix(self) -> str:
        return f"{self.safe_name}_"


@dataclass(slots=True)
class GatewaySettings:
    mode: str = "legacy"
    search_tool_name: str = "search_gateway_tools"
    call_tool_name: str = "call_gateway_tool"
    describe_tool_name: str = "describe_gateway_tool"
    max_results: int = 8
    always_visible: list[str] = field(default_factory=list)
    probe_timeout_sec: float = 3.0
    status_preview_count: int = 3
    error_max_chars: int = 280
    search_optional_fields_limit: int = 6
    search_description_max_chars: int = 96
    legacy_list_max_items: int = 24
    call_result_max_chars: int = 0
    tool_index_ttl_sec: float = 30.0


@dataclass(slots=True)
class GatewayConfig:
    settings: GatewaySettings
    servers: list[ServerConfig]


def _truncate(text: str | None, max_chars: int) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 3]}..."


def _one_line(text: str | None) -> str:
    line = (text or "无描述").strip().splitlines()[0]
    return line if len(line) <= 100 else f"{line[:97]}..."


def _expand_value(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _looks_like_path(value: str) -> bool:
    if not value:
        return False
    if value.startswith((".", "~")):
        return True
    if "/" in value or "\\" in value:
        return True
    return value.endswith((".py", ".sh", ".exe", ".bat", ".cmd"))


def _resolve_command(value: str, *, base_dir: Path) -> str:
    expanded = _expand_value(value)
    if _looks_like_path(expanded) and not os.path.isabs(expanded):
        return str((base_dir / expanded).resolve())
    return expanded


def _resolve_arg(value: str, *, base_dir: Path) -> str:
    expanded = _expand_value(value)
    if value.startswith("-"):
        return expanded
    if _looks_like_path(expanded) and not os.path.isabs(expanded):
        return str((base_dir / expanded).resolve())
    return expanded


def _load_config(config_path: Path | str) -> GatewayConfig:
    config_path = Path(config_path).expanduser().resolve()
    base_dir = config_path.parent

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    gateway_cfg = data.get("gateway", {})
    settings = GatewaySettings(
        mode=gateway_cfg.get("mode", "legacy"),
        search_tool_name=gateway_cfg.get("search_tool_name", "search_gateway_tools"),
        call_tool_name=gateway_cfg.get("call_tool_name", "call_gateway_tool"),
        describe_tool_name=gateway_cfg.get("describe_tool_name", "describe_gateway_tool"),
        max_results=int(gateway_cfg.get("max_results", 8)),
        always_visible=list(gateway_cfg.get("always_visible", [])),
        probe_timeout_sec=float(gateway_cfg.get("probe_timeout_sec", 3.0)),
        status_preview_count=int(gateway_cfg.get("status_preview_count", 3)),
        error_max_chars=int(gateway_cfg.get("error_max_chars", 280)),
        search_optional_fields_limit=int(gateway_cfg.get("search_optional_fields_limit", 6)),
        search_description_max_chars=int(gateway_cfg.get("search_description_max_chars", 96)),
        legacy_list_max_items=int(gateway_cfg.get("legacy_list_max_items", 24)),
        call_result_max_chars=int(gateway_cfg.get("call_result_max_chars", 0)),
        tool_index_ttl_sec=float(gateway_cfg.get("tool_index_ttl_sec", 30.0)),
    )

    servers = []
    for name, cfg in data.get("mcpServers", {}).items():
        servers.append(
            ServerConfig(
                name=name,
                command=_resolve_command(cfg["command"], base_dir=base_dir),
                args=[_resolve_arg(arg, base_dir=base_dir) for arg in cfg.get("args", [])],
                env={k: _expand_value(v) for k, v in cfg.get("env", {}).items()},
                profiles=list(cfg.get("profiles", [])),
                disabled=cfg.get("disabled", False),
            )
        )

    return GatewayConfig(settings=settings, servers=servers)


async def _fetch_tools(server: ServerConfig, *, timeout_sec: float) -> list[Any]:
    async with Client(server.client_config) as client:
        return await asyncio.wait_for(client.list_tools(), timeout=timeout_sec)


def _required_fields(tool: Any) -> list[str]:
    schema = getattr(tool, "inputSchema", None) or {}
    required = schema.get("required", [])
    return [str(item) for item in required]


def _optional_fields(tool: Any) -> list[str]:
    schema = getattr(tool, "inputSchema", None) or {}
    props = schema.get("properties", {}) or {}
    required = set(_required_fields(tool))
    return [str(name) for name in props.keys() if name not in required]


def _tool_line(tool: Any, *, desc_max_chars: int) -> str:
    required = _required_fields(tool)
    required_hint = f" required={required}" if required else ""
    desc = _truncate(_one_line(getattr(tool, "description", None)), desc_max_chars)
    return f"{tool.name}{required_hint}: {desc}"


def _argument_template(tool: Any) -> dict[str, str]:
    """只返回必填参数模板，帮助模型把参数放进 arguments 而不是乱造 list/params。"""

    return {name: "<required>" for name in _required_fields(tool)}


def _compact_tool_summary(
    tool: Any,
    *,
    settings: GatewaySettings,
    exposed_name: str | None = None,
) -> dict[str, Any]:
    optional = _optional_fields(tool)
    optional_preview = optional[: settings.search_optional_fields_limit]
    summary = {
        "name": exposed_name or tool.name,
        "description": _truncate(
            _one_line(getattr(tool, "description", None)),
            settings.search_description_max_chars,
        ),
        "required": _required_fields(tool),
        "optional_preview": optional_preview,
        "optional_remaining": max(0, len(optional) - len(optional_preview)),
    }
    template = _argument_template(tool)
    if template:
        summary["arguments_template"] = template
    return summary


async def _probe_server(
    server: ServerConfig,
    *,
    settings: GatewaySettings,
) -> dict[str, Any]:
    exposed_prefix = f"{server.safe_name}_"
    if server.disabled:
        return {
            "name": server.name,
            "prefix": exposed_prefix,
            "profiles": server.profiles,
            "disabled": True,
            "status": "disabled",
            "tool_count": 0,
            "tools_preview": [],
            "init_error": None,
            "command": server.command,
            "args": server.args,
        }

    try:
        tools = await _fetch_tools(server, timeout_sec=settings.probe_timeout_sec)
        preview = [
            f"{exposed_prefix}{tool.name}: "
            f"{_truncate(_one_line(tool.description), settings.search_description_max_chars)}"
            for tool in tools[: settings.status_preview_count]
        ]
        return {
            "name": server.name,
            "prefix": exposed_prefix,
            "profiles": server.profiles,
            "disabled": False,
            "status": "ok",
            "tool_count": len(tools),
            "tools_preview": preview,
            "init_error": None,
            "command": server.command,
            "args": server.args,
        }
    except Exception as exc:
        return {
            "name": server.name,
            "prefix": exposed_prefix,
            "profiles": server.profiles,
            "disabled": False,
            "status": "degraded",
            "tool_count": 0,
            "tools_preview": [],
            "init_error": _truncate(str(exc), settings.error_max_chars),
            "command": server.command,
            "args": server.args,
        }


def _compact_server_status(status: dict[str, Any], *, verbose: bool) -> dict[str, Any]:
    compact = {
        "name": status["name"],
        "prefix": status["prefix"],
        "profiles": status["profiles"],
        "status": status["status"],
        "tool_count": status["tool_count"],
    }
    if status.get("init_error"):
        compact["init_error"] = status["init_error"]
    if verbose:
        compact["disabled"] = status["disabled"]
        compact["tools_preview"] = status["tools_preview"]
        compact["command"] = status["command"]
        compact["args"] = status["args"]
    return compact


async def _collect_exposed_tools(
    config: GatewayConfig,
    *,
    profile: str | None = None,
    server_name: str | None = None,
) -> list[tuple[ServerConfig, Any, str]]:
    collected: list[tuple[ServerConfig, Any, str]] = []
    for server in config.servers:
        if server.disabled:
            continue
        if not _matches_filter(server, profile=profile, server_name=server_name):
            continue
        try:
            tools = await _fetch_tools(server, timeout_sec=config.settings.probe_timeout_sec)
        except Exception:
            continue
        for tool in tools:
            collected.append((server, tool, f"{server.safe_name}_{tool.name}"))
    return collected


def _tokenize_query(query: str) -> list[str]:
    return [part for part in re.split(r"[^0-9a-zA-Z_]+", query.lower()) if part]


def _score_tool_match(query: str, tokens: list[str], exposed_name: str, description: str) -> int:
    name_text = exposed_name.lower()
    desc_text = description.lower()
    score = 0
    q = query.strip().lower()
    if q:
        if q in name_text:
            score += 20
        if q in desc_text:
            score += 10
    for token in tokens:
        if token in name_text:
            score += 6
        if token in desc_text:
            score += 3
    return score


def _matches_filter(server: ServerConfig, *, profile: str | None, server_name: str | None) -> bool:
    if server_name:
        wanted = server_name.lower()
        if server.name.lower() != wanted and server.safe_name.lower() != wanted:
            return False
    if profile and profile.lower() not in {item.lower() for item in server.profiles}:
        return False
    return True


class ToolIndex:
    """短生命周期工具索引，避免每次 search/describe/call 都扫所有上游。"""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self._items: list[tuple[ServerConfig, Any, str]] = []
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def items(
        self,
        *,
        force_refresh: bool = False,
        profile: str | None = None,
        server_name: str | None = None,
    ) -> list[tuple[ServerConfig, Any, str]]:
        if profile or server_name:
            return await _collect_exposed_tools(
                self.config,
                profile=profile,
                server_name=server_name,
            )

        now = time.monotonic()
        if (
            not force_refresh
            and self._items
            and now - self._loaded_at < self.config.settings.tool_index_ttl_sec
        ):
            return self._items

        async with self._lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._items
                and now - self._loaded_at < self.config.settings.tool_index_ttl_sec
            ):
                return self._items

            self._items = await _collect_exposed_tools(self.config)
            self._loaded_at = time.monotonic()
            return self._items

    async def find(self, name: str) -> tuple[ServerConfig, Any, str] | None:
        hinted_server = self._server_from_exposed_name(name)
        if hinted_server is not None:
            for item in await self.items(server_name=hinted_server.name):
                if item[2] == name:
                    return item

        for item in await self.items():
            if item[2] == name:
                return item
        for item in await self.items(force_refresh=True):
            if item[2] == name:
                return item
        return None

    def _server_from_exposed_name(self, name: str) -> ServerConfig | None:
        matches = [
            server
            for server in self.config.servers
            if not server.disabled and name.startswith(server.tool_prefix)
        ]
        if not matches:
            return None
        return max(matches, key=lambda server: len(server.tool_prefix))


async def _find_tool_detail(
    config: GatewayConfig,
    exposed_tool_name: str,
) -> dict[str, Any] | None:
    for server in config.servers:
        if server.disabled:
            continue
        try:
            tools = await _fetch_tools(server, timeout_sec=config.settings.probe_timeout_sec)
        except Exception:
            continue
        for tool in tools:
            exposed_name = f"{server.safe_name}_{tool.name}"
            if exposed_name == exposed_tool_name:
                return {
                    "name": exposed_name,
                    "server": server.name,
                    "upstream_tool": tool.name,
                    "description": getattr(tool, "description", None) or "",
                    "required": _required_fields(tool),
                    "optional": _optional_fields(tool),
                    "input_schema": getattr(tool, "inputSchema", None),
                    "output_schema": getattr(tool, "outputSchema", None),
                }
    return None


def _validate_arguments(tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    supplied = set(arguments.keys())
    missing = sorted(required - supplied)
    unexpected: list[str] = []
    if schema.get("additionalProperties") is False:
        unexpected = sorted(supplied - set(props.keys()))
    if not missing and not unexpected:
        return {"ok": True}
    return {
        "ok": False,
        "missing": missing,
        "unexpected": unexpected,
        "required": sorted(required),
        "allowed": sorted(props.keys()),
    }


class LegacyGateway:
    """兼容旧 use_<server>(action, params) 风格的网关"""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.app = FastMCP(name="MCP-Gateway")
        self._tools_cache: dict[str, list[str]] = {}
        self._register_gateway_tools()
        self._register_server_tools()

    def _register_gateway_tools(self) -> None:
        @self.app.tool(
            name="gateway_status",
            description="查看网关已加载的子服务、工具数量和初始化状态",
        )
        async def gateway_status(verbose: bool = False) -> dict[str, Any]:
            raw_servers = await asyncio.gather(
                *[
                    _probe_server(
                        server,
                        settings=self.config.settings,
                    )
                    for server in self.config.servers
                ]
            )
            servers = [
                _compact_server_status(server, verbose=verbose) for server in raw_servers
            ]
            return {"server_count": len(servers), "servers": servers}

    def _register_server_tools(self) -> None:
        for server in self.config.servers:
            if server.disabled:
                _log(f"跳过已禁用子服务: {server.name}")
                continue

            @self.app.tool(
                name=f"use_{server.name}",
                description=self._build_description(server),
            )
            async def dispatch(
                action: str,
                params: dict[str, Any] | None = None,
                _server: ServerConfig = server,
            ) -> str:
                return await self._handle_dispatch(_server, action, params or {})

    def _build_description(self, server: ServerConfig) -> str:
        return f"""与 **{server.name}** 子系统交互。

参数：
- action: 要调用的工具名（用 \"list\" 查看可用工具）
- params: 工具参数字典
"""

    async def _handle_dispatch(
        self,
        server: ServerConfig,
        action: str,
        params: dict[str, Any],
    ) -> str:
        if action == "list":
            return await self._list_tools(server)
        return await self._call_tool(server, action, params)

    async def _list_tools(self, server: ServerConfig) -> str:
        if server.name not in self._tools_cache:
            try:
                tools = await _fetch_tools(
                    server,
                    timeout_sec=self.config.settings.probe_timeout_sec,
                )
                self._tools_cache[server.name] = [
                    _tool_line(
                        tool,
                        desc_max_chars=self.config.settings.search_description_max_chars,
                    )
                    for tool in tools
                ]
            except Exception as exc:
                return (
                    "❌ 无法获取工具列表: "
                    f"{_truncate(str(exc), self.config.settings.error_max_chars)}"
                )

        tools = self._tools_cache[server.name]
        shown = tools[: self.config.settings.legacy_list_max_items]
        body = "\n".join(f"  • {tool}" for tool in shown)
        more = ""
        if len(tools) > len(shown):
            more = f"\n\n... 还有 {len(tools) - len(shown)} 个工具未展开"
        return f"📦 [{server.name}] 可用工具 ({len(tools)} 个):\n\n{body}{more}"

    async def _call_tool(
        self,
        server: ServerConfig,
        action: str,
        params: dict[str, Any],
    ) -> str:
        try:
            async with Client(server.client_config) as client:
                result = await client.call_tool(action, params)
        except Exception as exc:
            return (
                f"❌ [{server.name}] 调用 `{action}` 失败: "
                f"{_truncate(str(exc), self.config.settings.error_max_chars)}"
            )
        return _extract_content(result)

    def run(self) -> None:
        self.app.run()


def _extract_content(result: Any) -> str:
    if not hasattr(result, "content") or not result.content:
        return str(result)

    parts = []
    for item in result.content:
        if hasattr(item, "text"):
            parts.append(item.text)
        elif hasattr(item, "data"):
            parts.append(str(item.data))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _extract_result(result: Any) -> str | dict[str, Any]:
    structured_content = getattr(result, "structured_content", None)
    if structured_content is not None:
        return structured_content
    return _extract_content(result)


def _create_search_gateway(config: GatewayConfig) -> FastMCP[Any]:
    app = FastMCP(name="MCP-Gateway")
    index = ToolIndex(config)

    @app.tool(
        name="gateway_status",
        description="查看网关已配置子服务的健康状态、前缀和工具数量",
    )
    async def gateway_status(
        verbose: bool = False,
        profile: str | None = None,
        server: str | None = None,
    ) -> dict[str, Any]:
        selected = [
            item
            for item in config.servers
            if _matches_filter(item, profile=profile, server_name=server)
        ]
        raw_servers = await asyncio.gather(
            *[
                _probe_server(
                    selected_server,
                    settings=config.settings,
                )
                for selected_server in selected
            ]
        )
        servers = [
            _compact_server_status(server, verbose=verbose) for server in raw_servers
        ]
        return {"server_count": len(servers), "servers": servers}

    @app.tool(
        name=config.settings.search_tool_name,
        description="搜索 gateway 下真实可调用的工具。返回紧凑结果：工具名、单行描述、必填参数、少量可选参数预览。",
    )
    async def search_gateway_tools(
        query: str,
        profile: str | None = None,
        server: str | None = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        tokens = _tokenize_query(query)
        tools = [
            item
            for item in await index.items(
                force_refresh=force_refresh,
                profile=profile,
                server_name=server,
            )
        ]
        ranked = []
        for _, tool, exposed_name in tools:
            description = getattr(tool, "description", None) or ""
            score = _score_tool_match(query, tokens, exposed_name, description)
            if score <= 0 and query.strip():
                continue
            ranked.append(
                (
                    score,
                    _compact_tool_summary(
                        tool,
                        settings=config.settings,
                        exposed_name=exposed_name,
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1]["name"]))
        results = [item[1] for item in ranked[: config.settings.max_results]]
        if results:
            return results
        fallback = sorted(
            (
                _compact_tool_summary(
                    tool,
                    settings=config.settings,
                    exposed_name=exposed_name,
                )
                for _, tool, exposed_name in tools
            ),
            key=lambda item: item["name"],
        )
        return fallback[: config.settings.max_results]

    @app.tool(
        name=config.settings.describe_tool_name,
        description="按工具名查看单个 gateway 工具的详细 schema。先用 search_gateway_tools 搜，再按需 describe。",
    )
    async def describe_gateway_tool(
        name: str,
        include_output_schema: bool = False,
    ) -> dict[str, Any]:
        item = await index.find(name)
        if item is None:
            return {
                "ok": False,
                "error": f"tool not found: {name}",
            }
        server, tool, exposed_name = item
        result = {
            "ok": True,
            "name": exposed_name,
            "server": server.name,
            "profiles": server.profiles,
            "upstream_tool": tool.name,
            "description": getattr(tool, "description", None) or "",
            "required": _required_fields(tool),
            "optional": _optional_fields(tool),
            "arguments_template": _argument_template(tool),
            "input_schema": getattr(tool, "inputSchema", None),
        }
        if include_output_schema:
            result["output_schema"] = getattr(tool, "outputSchema", None)
        return result

    @app.tool(
        name=config.settings.call_tool_name,
        description="调用 gateway 下某个真实工具。name 来自 search_gateway_tools / describe_gateway_tool，arguments 为该工具的真实参数字典。",
    )
    async def call_gateway_tool(
        name: str,
        arguments: dict[str, Any] | None = None,
        max_chars: int | None = None,
    ) -> str | dict[str, Any]:
        item = await index.find(name)
        if item is None:
            return {"ok": False, "error": f"tool not found: {name}"}
        target_server, tool, _ = item
        arguments = arguments or {}

        validation = _validate_arguments(tool, arguments)
        if not validation["ok"]:
            return {
                "ok": False,
                "error": "invalid arguments",
                "name": name,
                **validation,
            }

        try:
            async with Client(target_server.client_config) as client:
                result = await client.call_tool(
                    tool.name,
                    arguments,
                )
        except Exception as exc:
            return {
                "ok": False,
                "error": _truncate(str(exc), config.settings.error_max_chars),
                "name": name,
                "server": target_server.name,
            }
        payload = _extract_result(result)
        limit = config.settings.call_result_max_chars if max_chars is None else max_chars
        if limit and limit > 0:
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False, default=str)
            return _truncate(payload, limit)
        return payload

    return app


def create_gateway(
    config_path: Path | str | None = None,
    *,
    mode_override: str | None = None,
) -> LegacyGateway | FastMCP[Any]:
    if config_path is None:
        config_path = os.environ.get("MCP_GATEWAY_CONFIG")
    if config_path is None:
        config_path = Path(__file__).parent / "mcps_config.json"

    config = _load_config(config_path)
    if mode_override is not None:
        config.settings.mode = mode_override

    if config.settings.mode == "search":
        return _create_search_gateway(config)

    return LegacyGateway(config)


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Gateway")
    parser.add_argument(
        "--config",
        default=None,
        help="网关配置文件路径，默认读取 MCP_GATEWAY_CONFIG 或当前目录下 mcps_config.json",
    )
    parser.add_argument(
        "--mode",
        choices=("legacy", "search"),
        default=None,
        help="覆盖配置文件中的网关模式",
    )
    args = parser.parse_args()
    gateway = create_gateway(args.config, mode_override=args.mode)
    gateway.run()


if __name__ == "__main__":
    main()
