import asyncio
import shutil
from pathlib import Path

import httpx
import pytest

from ha_llm_context import pipeline, tools
from ha_llm_context.config import HASettings

pytest.importorskip("mcp")

from ha_llm_context.mcp_server import BearerAuth, build_server, http_app  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "run1"


def _fake_collect(settings, entities, start, end, out_dir, **kwargs):
    shutil.copytree(FIXTURE, out_dir)
    return out_dir


def _cfg(**kw):
    return tools.ToolConfig(HASettings("http://ha.invalid", "t"), **kw)


def _text(result) -> str:
    return "".join(c.text for c in result.content)


def test_tools_are_marked_read_only():
    listed = asyncio.run(build_server(_cfg()).list_tools())
    assert {t.name for t in listed} == {tools.FIND_NAME, tools.REFS_NAME, tools.TIMELINE_NAME}
    for t in listed:
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


def test_timeline_tool_returns_timeline(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "collect", _fake_collect)
    server = build_server(_cfg(out_root=tmp_path))
    result = asyncio.run(server.call_tool(tools.TIMELINE_NAME, {"entity_ids": "light.hallway"}))
    assert _text(result).startswith("# Home Assistant timeline")
    assert list(tmp_path.iterdir())  # raw pull kept


def test_errors_come_back_as_text(monkeypatch):
    server = build_server(_cfg())
    result = asyncio.run(server.call_tool(tools.TIMELINE_NAME, {"entity_ids": "porch light"}))
    assert _text(result).startswith("Error: Not entity IDs")

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(pipeline, "find_entities", boom)
    result = asyncio.run(server.call_tool(tools.FIND_NAME, {"query": "porch"}))
    assert _text(result) == "Error searching entities: connection refused"


def test_timeline_warns_when_large(monkeypatch):
    monkeypatch.setattr(pipeline, "collect", _fake_collect)
    out = tools.timeline(_cfg(warn_tokens=10), "light.hallway")
    assert out.ok and out.text.startswith("Note: this timeline is about")


# ---- bearer token in front of the HTTP app

async def _plain_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _get(app, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/mcp", headers=headers or {})


def test_bearer_auth():
    app = BearerAuth(_plain_app, "s3cret")
    assert asyncio.run(_get(app)).status_code == 401
    assert asyncio.run(_get(app, {"Authorization": "Bearer wrong"})).status_code == 401
    assert asyncio.run(_get(app, {"Authorization": "Bearer s3cret"})).status_code == 200


def test_http_app_only_wraps_when_token_set():
    server = build_server(_cfg())
    assert isinstance(http_app(server, "0.0.0.0", "s3cret"), BearerAuth)
    assert not isinstance(http_app(server, "0.0.0.0", ""), BearerAuth)
