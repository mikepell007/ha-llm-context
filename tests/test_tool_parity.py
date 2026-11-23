"""The Open WebUI tool and the MCP server must offer the model the same tools.

Open WebUI reads tool descriptions from the pasted file's docstrings, so that
file carries its own copy of the text in ha_llm_context/tools.py. These tests
fail when the copies drift apart.
"""

import asyncio
import importlib.util
import inspect
import re
from pathlib import Path

import pytest

from ha_llm_context import tools
from ha_llm_context.config import HASettings

pytest.importorskip("pydantic")
pytest.importorskip("mcp")

from ha_llm_context.mcp_server import build_server  # noqa: E402

TOOL = Path(__file__).parent.parent / "openwebui" / "ha_timeline_tool.py"

SHARED = {
    tools.FIND_NAME: (tools.FIND_DESCRIPTION, tools.FIND_PARAMS, tools.FIND_RETURNS),
    tools.TIMELINE_NAME: (tools.TIMELINE_DESCRIPTION, tools.TIMELINE_PARAMS, tools.TIMELINE_RETURNS),
}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_docstring(doc: str) -> tuple[str, dict[str, str], str]:
    """Split an Open WebUI docstring into description, :param texts, and :return text."""
    description, params, returns = [], {}, ""
    current = None
    for line in inspect.cleandoc(doc).splitlines():
        if m := re.match(r":param (\w+):\s*(.*)", line):
            current = m.group(1)
            params[current] = m.group(2)
        elif m := re.match(r":return:\s*(.*)", line):
            current = "__return__"
            returns = m.group(1)
        elif current == "__return__":
            returns += " " + line
        elif current:
            params[current] += " " + line
        else:
            description.append(line)
    return (_squash(" ".join(description)), {k: _squash(v) for k, v in params.items()},
            _squash(returns))


@pytest.fixture(scope="module")
def openwebui_tools():
    spec = importlib.util.spec_from_file_location("ha_timeline_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = mod.Tools
    return {name: fn for name, fn in vars(cls).items()
            if inspect.iscoroutinefunction(fn) and not name.startswith("_")}


@pytest.fixture(scope="module")
def mcp_tools():
    server = build_server(tools.ToolConfig(HASettings("http://ha.invalid", "t")))
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_same_tool_names(openwebui_tools, mcp_tools):
    assert set(openwebui_tools) == set(mcp_tools) == set(SHARED)


@pytest.mark.parametrize("name", SHARED)
def test_openwebui_docstrings_match_shared_text(openwebui_tools, name):
    description, params, returns = _parse_docstring(openwebui_tools[name].__doc__)
    want_description, want_params, want_returns = SHARED[name]
    assert description == _squash(want_description)
    assert params == {k: _squash(v) for k, v in want_params.items()}
    assert returns == _squash(want_returns)


@pytest.mark.parametrize("name", SHARED)
def test_same_parameters_and_defaults(openwebui_tools, mcp_tools, name):
    sig = inspect.signature(openwebui_tools[name])
    owui = {p.name: p.default for p in sig.parameters.values()
            if p.name != "self" and not p.name.startswith("__")}
    schema = mcp_tools[name].input_schema
    props = schema["properties"]
    mcp = {k: v.get("default", inspect.Parameter.empty) for k, v in props.items()}
    assert owui == mcp
    assert set(schema.get("required", [])) == {k for k, v in owui.items()
                                               if v is inspect.Parameter.empty}
    for param, text in SHARED[name][1].items():
        assert props[param]["description"] == text


@pytest.mark.parametrize("name", SHARED)
def test_mcp_descriptions_use_shared_text(mcp_tools, name):
    description, _, returns = SHARED[name]
    assert mcp_tools[name].description == f"{description} Returns: {returns}"
