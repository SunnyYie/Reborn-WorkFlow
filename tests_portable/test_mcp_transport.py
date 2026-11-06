"""Real SDK client/server stdio, with isolated test state and no live PCO calls."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(HAS_MCP, "Optional MCP SDK not installed; install requirements-agent.txt for transport acceptance")
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_handshake_tools_resources_and_confirmation_barrier(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        (ROOT / "workflow/runs").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "workflow/runs", prefix="mcp-test-") as directory:
            session_path = Path(directory) / "state.json"
            sys.path.insert(0, str(ROOT / "workflow"))
            import engine
            state = engine.new_state()
            state["test_run"] = True
            # A referenced fixture is only transport bytes, not a playable MP3.
            audio = Path(directory) / "fixture.mp3"
            audio.write_bytes(b"isolated resource transport fixture, not an MP3")
            state["transport_fixture"] = {"path": str(audio)}
            sources = Path(directory) / "fixture.sources.json"
            sources.write_text('{"fixture": true}', encoding="utf-8")
            state["source_fixture"] = {"path": str(sources)}
            credentials = Path(directory) / "credentials.json"
            credentials.write_text('{"fixture": "must not be served"}', encoding="utf-8")
            state["forbidden_fixture"] = {"path": str(credentials)}
            session_path.write_text(json.dumps(state), encoding="utf-8")
            parameters = StdioServerParameters(
                command=sys.executable, args=["-B", str(ROOT / "reborn.py"), "mcp"],
                cwd=str(ROOT), env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.serverInfo.name, "Reborn Worship Workflow")
                    tools = {tool.name for tool in (await client.list_tools()).tools}
                    self.assertIn("workflow_execute", tools)
                    self.assertIn("workflow_next", tools)
                    result = await client.call_tool("workflow_guide", {"name": "workflow/SKILL.md"})
                    self.assertFalse(result.isError)
                    self.assertIn("key-confirm", result.content[0].text)

                    def data(result):
                        self.assertFalse(result.isError, result)
                        return json.loads(result.content[0].text)

                    result = data(await client.call_tool("workflow_next", {"state_path": str(session_path)}))
                    self.assertEqual(result["result"]["action"], "collect_requirements")
                    before = session_path.read_bytes()
                    result = data(await client.call_tool("workflow_execute", {
                        "command": "key-confirm", "state_path": str(session_path),
                        "payload": {"confirmed": True, "listened": True,
                                    "user_message": "AUTOMATION ONLY invalid bypass"},
                    }))
                    self.assertFalse(result["ok"])
                    self.assertEqual(session_path.read_bytes(), before)
                    result = data(await client.call_tool("workflow_execute", {
                        "command": "requirements", "state_path": str(session_path),
                        "payload": {"theme": "isolated test", "slots": ["slow"] * 3, "preferences": []},
                    }))
                    self.assertTrue(result["ok"])
                    assets = data(await client.call_tool("workflow_assets", {"state_path": str(session_path)}))
                    self.assertEqual(len(assets["assets"]), 2)
                    audio_asset = next(item for item in assets["assets"] if item["mime_type"] == "audio/mpeg")
                    response = await client.read_resource(audio_asset["uri"])
                    self.assertEqual(response.contents[0].mimeType, "audio/mpeg")
                    self.assertEqual(base64.b64decode(response.contents[0].blob), audio.read_bytes())
                    source_asset = next(item for item in assets["assets"] if item["mime_type"] == "application/json")
                    response = await client.read_resource(source_asset["uri"])
                    self.assertEqual(response.contents[0].mimeType, "application/json")
                    self.assertEqual(base64.b64decode(response.contents[0].blob), sources.read_bytes())
                    rejected = await client.call_tool("workflow_guide", {"name": "../.env"})
                    self.assertTrue(rejected.isError)


if __name__ == "__main__":
    unittest.main()
