import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import downloads
import engine
import keys
import spoken
import test_support as support
import workflow
from pco_api import WorkflowError
from test_workflow import LYRICS, confirmed
from test_spoken import draft


class KeyTests(unittest.TestCase):
    def pending(self):
        state = confirmed()
        keys.requirements(state, support.VOCALS)
        state["key_catalog"] = {str(i): support.catalog(str(i)) for i in (1, 2, 3)}
        keys.propose(state, support.proposal())
        return state

    def approval(self, state):
        return {"proposal_id": "K1", "revision": state["keys"]["revision"],
                "confirmed": True, "listened": True, "user_message": "已听完三首，确认这些调"}

    def test_song_confirmation_alone_cannot_download(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(downloads, "inventory") as query:
            with self.assertRaises(WorkflowError):
                downloads.download(confirmed(), Mock(), temp)
            query.assert_not_called()
            self.assertFalse(list(Path(temp).iterdir()))

    def test_counts_and_registers_must_be_complete_and_consistent(self):
        for change in (
            {"male_count": 2}, {"female_count": -1}, {"male_count": True},
            {"profiles": []}, {"profiles": [{"id": "m", "gender": "male", "register": "unknown", "count": 1}]},
        ):
            with self.assertRaises(WorkflowError):
                keys.requirements(confirmed(), support.VOCALS | change)
        result = keys.requirements(confirmed(), support.VOCALS)
        self.assertEqual(result["requirements"]["female_count"], 1)

    def test_requires_existing_key_specific_mp3_and_confirmed_order(self):
        for field, value in (("key_id", "999"), ("attachment_id", "1002")):
            state = self.pending()
            payload = support.proposal()
            payload["proposals"][0]["songs"][0][field] = value
            with self.assertRaises(WorkflowError):
                keys.propose(state, payload)
        state = self.pending()
        payload = support.proposal()
        payload["proposals"][0]["songs"].reverse()
        with self.assertRaises(WorkflowError):
            keys.propose(state, payload)

    def test_restricted_mp3_not_substituted(self):
        state = self.pending()
        state["key_catalog"]["1"][0]["audios"][0]["mp3_allowed"] = False
        with self.assertRaises(WorkflowError):
            keys.propose(state, support.proposal())

    def test_cannot_confirm_unprepared_or_unlistened_or_stale(self):
        state = self.pending()
        with self.assertRaises(WorkflowError):
            keys.confirm(state, self.approval(state))
        with tempfile.TemporaryDirectory() as temp, patch("keys.inventory", side_effect=support.inventory), patch(
            "keys.validate_mp3"
        ):
            api = Mock()
            api.file.return_value = support.FAKE_MP3
            keys.preview(state, {"proposal_id": "K1"}, api, temp)
            for bad in ({"listened": False}, {"revision": 0}, {"user_message": ""}):
                with self.assertRaises(WorkflowError):
                    keys.confirm(state, self.approval(state) | bad)
            self.assertFalse(state["keys"]["previews"]["K1"]["1"]["listened"])
            keys.confirm(state, self.approval(state))
            self.assertTrue(state["keys"]["previews"]["K1"]["1"]["listened"])

    def test_prepared_file_is_not_an_automatic_human_approval(self):
        state = self.pending()
        with tempfile.TemporaryDirectory() as temp, patch("keys.inventory", side_effect=support.inventory), patch(
            "keys.validate_mp3"
        ):
            api = Mock()
            api.file.return_value = support.FAKE_MP3
            records = keys.preview(state, {"proposal_id": "K1"}, api, temp)
            self.assertTrue(all(r["status"] == "prepared" for r in records.values()))
            self.assertIsNone(state["keys"]["confirmation"])
            self.assertEqual(state["stage"], "awaiting_key_audition")
            with self.assertRaises(WorkflowError):
                downloads.download(state, api, Path(temp) / "final")
            self.assertFalse((Path(temp) / "final").exists())

    def test_changed_voice_profile_invalidates_audio_and_spoken_not_song_approval(self):
        state = support.keyed(confirmed())
        spoken.requirements(state, {})
        spoken.propose(state, draft())
        state["results"] = {
            "1": {"kind": "chinese_lyrics", "path": "lyrics.txt", "audio": {"status": "downloaded"}},
            "2": {"kind": "numbered_score", "path": "old-G.pdf", "audio": {"status": "downloaded"}},
        }
        song_confirmation = copy.deepcopy(state["confirmation"])
        keys.requirements(state, {"notes": "改由女声主领"})
        self.assertEqual(state["confirmation"], song_confirmation)
        self.assertEqual(state["results"]["1"]["path"], "lyrics.txt")
        self.assertNotIn("audio", state["results"]["1"])
        self.assertEqual(state["results"]["2"]["status"], "needs_review")
        self.assertEqual(state["spoken"]["stage"], "stale")
        with self.assertRaises(WorkflowError):
            keys.confirmed(state)

    def test_song_change_invalidates_keys(self):
        state = support.keyed(confirmed())
        engine.feedback(state, {"message": "换第一首"})
        with self.assertRaises(WorkflowError):
            keys.confirmed(state)
        self.assertFalse(state["key_catalog"])

    def test_key_feedback_preserved_and_old_approval_revoked(self):
        state = support.keyed(confirmed())
        keys.feedback(state, {"message": "第二首太高"})
        self.assertTrue(state["keys"]["history"])
        self.assertEqual(state["keys"]["feedback"][0]["message"], "第二首太高")
        with self.assertRaises(WorkflowError):
            keys.confirmed(state)

    def test_metadata_refresh_failure_does_not_leave_old_approval_usable(self):
        state = support.keyed(confirmed())
        with patch("keys.inventory", side_effect=WorkflowError("network_error", "Cannot verify")):
            result = keys.refresh(state, Mock())
        self.assertEqual(len(result["errors"]), 3)
        with self.assertRaises(WorkflowError):
            keys.confirmed(state)

    def test_key_detail_links_drive_inventory_not_audio_filename_guesses(self):
        song = {"song_id": "1", "arrangement_id": "11", "bpm": 68, "meter": "4/4"}
        api = Mock()
        root = "/services/v2/songs/1/arrangements/11"
        key_path = root + "/keys/101"
        api.request.side_effect = [
            {"data": {"attributes": {"bpm": 68, "meter": "4/4"}, "links": {}}},
            {"data": {"id": "101", "attributes": {"starting_key": "G", "starting_minor": False},
                      "links": {"attachments": key_path + "/attachments"}}},
        ]
        api.pages.side_effect = [
            [{"id": "101", "links": {"self": key_path}}],
            [{"id": "1001", "attributes": {
                "filename": "original.mp3", "downloadable": True, "allow_mp3_download": True
            }}],
        ]
        result = keys.inventory(api, song)
        self.assertEqual(result[0]["label"], "G")
        self.assertEqual(result[0]["audios"][0]["attachment_id"], "1001")
        self.assertEqual(api.pages.call_args.args[0], key_path + "/attachments")

    def test_no_attachment_link_is_unverified_not_a_matching_audio(self):
        api = Mock()
        api.request.side_effect = [
            {"data": {"attributes": {"bpm": 68, "meter": "4/4"}}},
            {"data": {"id": "101", "attributes": {"starting_key": "G"}, "links": {}}},
        ]
        api.pages.return_value = [{"id": "101", "links": {}}]
        result = keys.inventory(api, {"song_id": "1", "arrangement_id": "11", "bpm": 68, "meter": "4/4"})
        self.assertFalse(result[0]["audio_checked"])
        self.assertEqual(result[0]["audios"], [])

    def test_other_key_scores_are_not_substituted_even_after_classification(self):
        source = {"id": "attachment:9", "kind": "unknown", "key_id": "999", "scope": "selected",
                  "downloadable": True}
        review = {"classifications": {source["id"]: {
            "kind": "numbered_score", "evidence": "score", "fingerprint": downloads.source_fingerprint(source)
        }}}
        self.assertIsNone(downloads.choose([source], review, "101"))

    def test_metadata_drift_blocks_audio_delivery(self):
        state = support.keyed(confirmed())
        def changed(api, song):
            result = support.inventory(api, song)
            result[0]["audios"][0]["updated_at"] = "changed"
            return result
        with tempfile.TemporaryDirectory() as temp, patch("keys.inventory", side_effect=changed):
            keys.deliver(state, Mock(), temp)
            self.assertTrue(all(r["audio"]["status"] != "downloaded" for r in state["results"].values()))
            self.assertFalse(list(Path(temp).iterdir()))

    def test_changed_mp3_bytes_cannot_replace_what_user_auditioned(self):
        state = support.keyed(confirmed())  # Temporary preview files have been removed.
        api = Mock()
        api.file.return_value = b"changed bytes with unchanged server metadata"
        with tempfile.TemporaryDirectory() as temp, patch("keys.inventory", side_effect=support.inventory):
            keys.deliver(state, api, temp)
            self.assertTrue(all(r["audio"]["status"] == "needs_review" for r in state["results"].values()))
            self.assertFalse(list(Path(temp).iterdir()))

    def test_one_batch_delivers_audio_even_if_chinese_document_is_missing(self):
        state = confirmed()
        with tempfile.TemporaryDirectory() as temp:
            support.approve(state, Path(temp) / "previews")
            api = Mock()
            with patch("downloads.inventory", return_value=[]), patch(
                "keys.inventory", side_effect=support.inventory
            ), patch("keys.validate_mp3"):
                results = downloads.download(state, api, Path(temp) / "final")
                self.assertTrue(all(r["status"] == "missing" and r["audio"]["status"] == "downloaded"
                                    for r in results.values()))
                self.assertEqual(len(list((Path(temp) / "final").iterdir())), 3)
                downloads.download(state, api, Path(temp) / "final")
                api.file.assert_not_called()  # Reused exact auditioned bytes, no duplicate /open.

    def test_full_new_cli_flow_singers_options_audition_confirm_bundle(self):
        def call(args, payload=None):
            output = io.StringIO()
            with patch.object(sys, "argv", ["workflow.py", *args]), patch.object(
                sys, "stdin", io.StringIO(json.dumps(payload or {}, ensure_ascii=False))
            ), patch.object(sys, "stdout", output):
                workflow.main()
            return json.loads(output.getvalue())
        api = Mock()
        api.file.return_value = support.FAKE_MP3
        with tempfile.TemporaryDirectory() as temp, patch("workflow.PCO", return_value=api), patch(
            "keys.inventory", side_effect=support.inventory
        ), patch("keys.validate_mp3"), patch("downloads.inventory", return_value=[]):
            path = Path(temp) / "state.json"
            workflow.persist(path, confirmed())
            args = ["--state", str(path)]
            call(args + ["key-requirements"], support.VOCALS)
            call(args + ["key-options"])
            proposed = call(args + ["key-propose"], support.proposal())
            self.assertEqual(proposed["stage"], "awaiting_audition")
            previews = call(args + ["key-preview"], {"proposal_id": "K1"})
            self.assertFalse(previews["1"]["listened"])
            approved = call(args + ["key-confirm"], {
                "proposal_id": "K1", "revision": proposed["revision"], "confirmed": True,
                "listened": True, "user_message": "三首都听过，确认这些调",
            })
            self.assertTrue(approved["previews"]["K1"]["1"]["listened"])
            results = call(args + ["download"])
            self.assertEqual(len(results), 3)
            self.assertTrue(all(Path(r["audio"]["path"]).is_file() for r in results.values()))
            self.assertEqual(api.file.call_count, 3)
            self.assertEqual(json.loads(path.read_text())["stage"], "completed")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_real_codec_validation_with_generated_audio_not_a_pco_song(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.mp3"
            subprocess.run(
                [shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=0.2", "-codec:a", "libmp3lame", str(path)],
                capture_output=True, check=True, timeout=30,
            )
            keys.validate_mp3(path.read_bytes())
        with self.assertRaises(WorkflowError):
            keys.validate_mp3(b"<html>not an MP3</html>")


if __name__ == "__main__":
    unittest.main()
