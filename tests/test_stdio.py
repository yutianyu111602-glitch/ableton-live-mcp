"""Real stdio MCP handshake/tool-call test, without contacting a running Live set."""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_handshake_and_readonly_capability_call():
    async def exercise():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ableton_live_mcp.server"],
            env={
                **os.environ,
                "ABLETON_TOOLSETS": "analysis,lom",
                "ABLETON_HOST": "127.0.0.1",
                "ABLETON_PORT": "1",
            },
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                info = await session.initialize()
                assert info.serverInfo.name == "Ableton Live MCP Server"
                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                assert {
                    "analyze_audio_file",
                    "get_test_context",
                    "get_clip_snapshot",
                    "modify_notes_by_id",
                    "get_parameter_samples",
                } <= tool_names
                assert {
                    "lom_describe",
                    "lom_resolve",
                    "lom_get",
                    "lom_set",
                    "lom_call",
                    "lom_observe",
                    "lom_poll",
                    "lom_unobserve",
                    "lom_capabilities",
                } <= tool_names
                catalogue = await session.call_tool("lom_describe", {})
                assert not catalogue.isError
                assert '"member_count": 588' in catalogue.content[0].text
                result = await session.call_tool("describe_capabilities", {})
                assert not result.isError
                assert "groups" in result.content[0].text

    asyncio.run(asyncio.wait_for(exercise(), timeout=30))
