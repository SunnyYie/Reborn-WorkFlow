import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine as e
import spoken
import workflow
import test_support
from pco_api import WorkflowError
from test_workflow import prepared, proposal


def song_state():
    state = prepared()
    for i, title in enumerate(("我呼求", "让我", "如你"), 1):
        state["candidates"][f"{i}:{i + 10}"]["title"] = title
    e.propose(state, proposal())
    e.confirm(state, {
        "revision": state["revision"], "proposal_id": "A", "confirmed": True,
        "user_message": "确认这个顺序",
    })
    return test_support.keyed(state)


def draft(slots=None):
    rows = []
    for slot in slots or spoken.default_slots():
        scripture = {
            "reference": "诗篇51:10", "translation": "和合本", "mode": "quotation",
            "quote": "神啊，求你为我造清洁的心，使我里面重新有正直的灵。",
            "source": "隔离测试夹具：模拟用户提供的原文，不代表在线核验", "verification": "user_provided",
        }
        rows.append({
            "slot_id": slot["id"], "opening": "我们向神呼求，也愿意祂更新我们的生命。",
            "body": scripture["quote"] if slot["scripture_required"] else "主啊，求祢带领我们的脚步。",
            "closing": "愿我们在每天的生活中，学习顺服祢。",
            "previous_connection": "承接上一首的呼求或更新，不重复报歌名。",
            "song_connection": "从祈求走向具体生命回应。", "delivery_note": "话语结束留白，再示意乐队。",
            "estimated_seconds": 25, "scriptures": [scripture] if slot["scripture_required"] else [],
            "unknowns": [],
        })
    return {"cues": rows}


def approved_state():
    state = song_state()
    spoken.requirements(state, {})
    spoken.propose(state, draft())
    spoken.confirm(state, {
        "revision": state["spoken"]["revision"], "confirmed": True,
        "user_message": "这版祷告和话语可以，确认",
    })
    return state


class SpokenTests(unittest.TestCase):
    def test_requires_actual_song_confirmation(self):
        with self.assertRaises(WorkflowError):
            spoken.requirements(prepared(), {})

    def test_legacy_schema_one_state_can_start_phase_two(self):
        state = song_state()
        self.assertNotIn("spoken", state)
        result = spoken.requirements(state, {})
        self.assertEqual(len(result["requirements"]["slots"]), 4)
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(result["requirements"]["bible_translation"], "和合本")

    def test_context_and_order_derived_from_confirmed_songs(self):
        state = song_state()
        spoken.requirements(state, {})
        payload = draft()
        payload["cues"].reverse()
        part = spoken.propose(state, payload)
        cues = part["draft"]["cues"]
        self.assertEqual(cues[0]["song_title"], "我呼求")
        self.assertEqual(cues[1]["previous_song_title"], "我呼求")
        self.assertEqual(cues[1]["song_title"], "让我")
        self.assertEqual(cues[2]["previous_song_title"], "让我")
        self.assertEqual(cues[3]["song_title"], "如你")
        self.assertIsNone(cues[3]["previous_song_id"])

    def test_song_confirmation_does_not_confirm_script(self):
        state = song_state()
        spoken.requirements(state, {})
        spoken.propose(state, draft())
        with self.assertRaises(WorkflowError):
            spoken.render(state)
        self.assertIsNotNone(state["confirmation"])

    def test_spoken_feedback_does_not_change_songs_or_download_results(self):
        state = approved_state()
        state["results"] = {"1": {"status": "downloaded", "path": "existing-file.txt"}}
        before = copy.deepcopy((state["confirmation"], state["results"], state["revision"], state["stage"]))
        spoken.feedback(state, {"message": "保留承接，但结尾不要带歌名"})
        self.assertEqual((state["confirmation"], state["results"], state["revision"], state["stage"]), before)
        self.assertIsNone(state["spoken"]["confirmation"])
        self.assertTrue(state["spoken"]["history"])

    def test_spoken_requirements_revoke_only_script_confirmation(self):
        state = approved_state()
        spoken.requirements(state, {"tone": "更简短"})
        self.assertIsNone(state["spoken"]["confirmation"])
        self.assertIsNotNone(state["confirmation"])

    def test_song_revision_invalidates_spoken_and_preserves_history(self):
        state = approved_state()
        e.feedback(state, {"message": "调整第二首"})
        self.assertEqual(state["spoken"]["stage"], "stale")
        with self.assertRaises(WorkflowError):
            spoken.render(state)
        e.propose(state, proposal())
        e.confirm(state, {"revision": state["revision"], "proposal_id": "A",
                          "confirmed": True, "user_message": "确认新歌单"})
        test_support.keyed(state)
        spoken.requirements(state, {})
        self.assertTrue(state["spoken"]["history"])
        self.assertIsNone(state["spoken"]["draft"])

    def test_missing_bridge_rejected(self):
        state = song_state()
        spoken.requirements(state, {})
        payload = draft()
        del payload["cues"][1]["previous_connection"]
        with self.assertRaises(WorkflowError):
            spoken.propose(state, payload)

    def test_title_announcement_rejected_but_ordinary_let_me_allowed(self):
        state = song_state()
        spoken.requirements(state, {})
        for closing in ("接下来一起唱《让我》。", "接下来唱如你。"):
            payload = draft()
            payload["cues"][1]["closing"] = closing
            with self.assertRaises(WorkflowError):
                spoken.propose(state, payload)
        payload = draft()
        payload["cues"][1]["closing"] = "主啊，求祢让我在生活中学习顺服。"
        spoken.propose(state, payload)

    def test_quotation_must_match_spoken_text_and_requested_translation(self):
        for change in ("quote", "translation"):
            state = song_state()
            spoken.requirements(state, {})
            payload = draft()
            verse = payload["cues"][1]["scriptures"][0]
            verse[change] = "与原文不同"
            with self.assertRaises(WorkflowError):
                spoken.propose(state, payload)

    def test_unverified_quotation_cannot_be_approved_even_with_acceptance(self):
        state = song_state()
        spoken.requirements(state, {})
        payload = draft()
        payload["cues"][1]["scriptures"][0]["verification"] = "unverified"
        spoken.propose(state, payload)
        with self.assertRaises(WorkflowError):
            spoken.confirm(state, {"revision": state["spoken"]["revision"], "confirmed": True,
                                   "user_message": "确认", "accept_unknowns": True})

    def test_missing_or_duplicate_locations_rejected(self):
        for change in ("missing", "duplicate"):
            state = song_state()
            spoken.requirements(state, {})
            payload = draft()
            if change == "missing":
                payload["cues"].pop()
            else:
                payload["cues"][1]["slot_id"] = payload["cues"][0]["slot_id"]
            with self.assertRaises(WorkflowError):
                spoken.propose(state, payload)

    def test_custom_locations_and_timing(self):
        state = song_state()
        slots = [{
            "id": "opening", "song_position": 1, "placement": "before_intro",
            "kind": "words", "scripture_required": False, "target_seconds": 20,
        }]
        spoken.requirements(state, {"slots": slots})
        payload = draft(slots)
        with self.assertRaises(WorkflowError):
            spoken.propose(state, payload)
        payload["cues"][0]["estimated_seconds"] = 15
        spoken.propose(state, payload)

    def test_stale_approval_unknowns_and_tamper_guard(self):
        state = song_state()
        spoken.requirements(state, {})
        payload = draft()
        payload["cues"][0]["unknowns"] = ["间奏长度未排练"]
        spoken.propose(state, payload)
        approval = {"revision": state["spoken"]["revision"], "confirmed": True, "user_message": "确认"}
        with self.assertRaises(WorkflowError):
            spoken.confirm(state, approval | {"revision": 0})
        with self.assertRaises(WorkflowError):
            spoken.confirm(state, approval)
        spoken.confirm(state, approval | {"accept_unknowns": True})
        self.assertIn("口述时长仅为估计", spoken.render(state))
        state["spoken"]["draft"]["cues"][0]["closing"] = "改过的话语"
        with self.assertRaises(WorkflowError):
            spoken.render(state)

    def test_reference_only_not_presented_as_verbatim_quote(self):
        state = song_state()
        spoken.requirements(state, {})
        payload = draft()
        verse = payload["cues"][1]["scriptures"][0]
        verse["mode"] = "paraphrase"
        with self.assertRaises(WorkflowError):
            spoken.propose(state, payload)
        verse.pop("quote")
        spoken.propose(state, payload)

    def test_cli_persistence_and_export_without_pco_network(self):
        def call(args, payload=None):
            output = io.StringIO()
            with patch.object(sys, "argv", ["workflow.py", *args]), patch.object(
                sys, "stdin", io.StringIO(json.dumps(payload or {}, ensure_ascii=False))
            ), patch.object(sys, "stdout", output):
                workflow.main()
            return json.loads(output.getvalue())

        with tempfile.TemporaryDirectory() as temp, patch.object(workflow, "PCO") as api:
            path = Path(temp) / "state.json"
            workflow.persist(path, song_state())
            args = ["--state", str(path)]
            call(args + ["spoken-requirements"])
            result = call(args + ["spoken-propose"], draft())
            call(args + ["spoken-confirm"], {
                "revision": result["revision"], "confirmed": True, "user_message": "确认话语稿",
            })
            exported = call(args + ["spoken-export"])
            self.assertIn("我呼求 → 让我 → 如你", Path(exported["path"]).read_text())
            self.assertIn("话语结束留白", Path(exported["path"]).read_text())
            self.assertEqual(exported, call(args + ["spoken-export"]))
            saved = json.loads(path.read_text())
            self.assertEqual(saved["spoken"]["stage"], "confirmed")
            self.assertEqual(len(saved["spoken"]["exports"]), 1)
            api.return_value.request.assert_not_called()
            api.return_value.file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
