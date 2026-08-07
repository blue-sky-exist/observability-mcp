from __future__ import annotations

import inspect

import pytest

import observability_mcp.app as app_module
from observability_mcp.__main__ import main
from observability_mcp.app import mcp


@pytest.mark.asyncio
async def test_mcpserver_generated_schema() -> None:
    assert callable(main)
    assert "default to the production environment" in mcp.instructions
    assert "Do not automatically retry" in mcp.instructions
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "list_log_indices",
        "search_logs",
        "list_skywalking_services",
        "search_skywalking_logs",
        "get_skywalking_trace",
        "query_instant",
        "query_range",
        "get_label_values",
    }
    list_indices = next(tool for tool in tools if tool.name == "list_log_indices")
    assert list_indices.input_schema["properties"]["limit"]["default"] == 200
    assert list_indices.input_schema["properties"]["target_type"]["default"] == "all"
    assert list_indices.input_schema["properties"]["include_system"]["default"] is False
    search = next(tool for tool in tools if tool.name == "search_logs")
    properties = search.input_schema["properties"]
    assert "profile" not in properties
    assert properties["node_ip"]["description"]
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 1000
    assert properties["limit"]["default"] == 50
    assert "_tool_manager" not in inspect.getsource(app_module)
