"""MCP server exposing the same read-only tools as the Open WebUI tool.

Run with `ha-llm-context mcp` (stdio, for clients that start the server
themselves) or `ha-llm-context mcp --http` (a long-running service that
clients such as Open WebUI connect to). Needs the `mcp` extra:
`pip install 'ha-llm-context[mcp]'`.

Home Assistant settings come from the environment or .env, as for the CLI.
"""

from __future__ import annotations

import hmac
import sys
from typing import Annotated, Any

from pydantic import Field

from . import tools


def build_server(cfg: tools.ToolConfig) -> Any:
    """An MCPServer with the tools registered against `cfg`."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer(
        name="ha-llm-context",
        instructions=(
            "Read-only Home Assistant troubleshooting. Find entity IDs, then get a "
            "condensed timeline and explain from it what happened and why, or list "
            "everything that refers to an entity. "
            "These tools cannot change Home Assistant."
        ),
    )
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False)

    def find_home_assistant_entities(
        query: Annotated[str, Field(description=tools.FIND_PARAMS["query"])],
        domain: Annotated[str, Field(description=tools.FIND_PARAMS["domain"])] = "",
    ) -> str:
        return tools.find(cfg, query, domain).text

    def find_home_assistant_references(
        entity_id: Annotated[str, Field(description=tools.REFS_PARAMS["entity_id"])],
    ) -> str:
        return tools.find_references(cfg, entity_id).text

    def get_home_assistant_timeline(
        entity_ids: Annotated[str, Field(description=tools.TIMELINE_PARAMS["entity_ids"])],
        since: Annotated[str, Field(description=tools.TIMELINE_PARAMS["since"])] = tools.DEFAULT_SINCE,
        until: Annotated[str, Field(description=tools.TIMELINE_PARAMS["until"])] = "",
    ) -> str:
        return tools.timeline(cfg, entity_ids, since, until).text

    server.add_tool(find_home_assistant_entities, name=tools.FIND_NAME,
                    description=f"{tools.FIND_DESCRIPTION} Returns: {tools.FIND_RETURNS}",
                    annotations=read_only, structured_output=False)
    server.add_tool(find_home_assistant_references, name=tools.REFS_NAME,
                    description=f"{tools.REFS_DESCRIPTION} Returns: {tools.REFS_RETURNS}",
                    annotations=read_only, structured_output=False)
    server.add_tool(get_home_assistant_timeline, name=tools.TIMELINE_NAME,
                    description=f"{tools.TIMELINE_DESCRIPTION} Returns: {tools.TIMELINE_RETURNS}",
                    annotations=read_only, structured_output=False)
    return server


class BearerAuth:
    """ASGI wrapper that rejects HTTP requests without `Authorization: Bearer <token>`.

    The server hands out your home's history to anyone who can reach it, so
    when it listens beyond localhost it should require a token.
    """

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"Unauthorized\n"})
                return
        await self.app(scope, receive, send)


def http_app(server: Any, host: str, token: str = "") -> Any:
    app = server.streamable_http_app(host=host)
    return BearerAuth(app, token) if token else app


def serve(cfg: tools.ToolConfig, http: bool = False, host: str = "127.0.0.1",
          port: int = 8765, token: str = "") -> None:
    try:
        server = build_server(cfg)
    except ImportError:
        raise SystemExit("The MCP server needs the mcp extra: pip install 'ha-llm-context[mcp]'")

    if not http:
        server.run("stdio")
        return

    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        print(f"Warning: listening on {host} without MCP_TOKEN. Anyone who can reach "
              "this port can read your Home Assistant history.", file=sys.stderr)
    print(f"MCP server on http://{host}:{port}/mcp", file=sys.stderr)
    uvicorn.run(http_app(server, host, token), host=host, port=port, log_level="warning")
