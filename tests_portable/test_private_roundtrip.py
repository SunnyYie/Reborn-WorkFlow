"""Optional private-template integration; never substitute a fake production template."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "skills/pco-assemble-worship-setlist/assets/reference.docx"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "workflow"))


@unittest.skipUnless(TEMPLATE.is_file(), "Private authorized template is not distributed in public checkouts")
class PrivateRoundtripTests(unittest.TestCase):
    def test_original_assembly_through_adapter_preserves_template_and_confirmation_barriers(self):
        from portable.bridge import Bridge
        import workflow
        from test_setlist import SetlistTests

        before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
        fixture = SetlistTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        selection = fixture.ready()
        for sid in ("1", "3"):
            fixture.state["results"][sid]["status"] = "reused"
        (ROOT / "workflow/runs").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "workflow/runs", prefix="private-adapter-test-") as directory:
            state = Path(directory) / "state.json"
            workflow.persist(state, fixture.state)
            bridge = Bridge()
            proposal = bridge.execute("setlist-propose", str(state), selection)
            self.assertTrue(proposal["ok"], proposal)
            self.assertEqual(bridge.execute("next", str(state))["result"]["action"], "review_and_confirm_setlist")
            self.assertFalse(bridge.execute("setlist-export", str(state))["ok"])
            draft = bridge.execute("setlist-export", str(state), draft=True)
            self.assertTrue(draft["ok"], draft)
            self.assertEqual(draft["result"]["status"], "test_artifact")
            revision = proposal["result"]["revision"]
            before_confirmation = state.read_bytes()
            rejected = bridge.execute("setlist-confirm", str(state), {
                "revision": revision - 1, "confirmed": True,
                "user_message": "AUTOMATION ONLY stale simulated approval"})
            self.assertFalse(rejected["ok"])
            self.assertEqual(state.read_bytes(), before_confirmation)
            approved = bridge.execute("setlist-confirm", str(state), {
                "revision": revision, "confirmed": True,
                "user_message": "AUTOMATION ONLY simulated current setlist approval"})
            self.assertTrue(approved["ok"], approved)
            exported = bridge.execute("setlist-export", str(state))
            self.assertTrue(exported["ok"], exported)
            result = exported["result"]
            self.assertTrue(Path(result["path"]).is_file())
            self.assertTrue(json.loads(Path(result["sources"]).read_text())["selection"]["test_run"])
            types = {item["mime_type"] for item in bridge.assets(str(state))["assets"]}
            self.assertIn("application/json", types)
            self.assertIn("application/vnd.openxmlformats-officedocument.wordprocessingml.document", types)
            self.assertFalse(bridge.execute("status", str(state))["result"]["delivery_complete"])
            self.assertEqual(bridge.execute("next", str(state))["result"]["actor"], "capable_reviewer")
        self.assertEqual(hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
