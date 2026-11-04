import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import downloads as dl
import engine as e
import pco_api
import workflow
import test_support
from pco_api import PCO, WorkflowError

LYRICS = "祢用重价救赎我使我脱离罪恶河\n叫我这不配的人凭信得救\n让我学习顺服以圣洁为装饰"
PDF = b"%PDF-1.4\noriginal numbered score\n%%EOF\n"


def prepared():
    state = e.new_state()
    e.requirements(state, {
        "theme": "迷茫、认识神、跟随", "slots": ["slow", "slow", "slow"],
        "preferences": [],
    })
    e.add_candidates(state, [
        {"song_id": str(i), "arrangement_id": str(i + 10), "title": f"Song {i}",
         "bpm": 68, "meter": "4/4", "lyrics": LYRICS}
        for i in range(1, 6)
    ])
    return state


def proposal(count=1):
    orders = [(1, 2, 3), (1, 4, 3), (2, 4, 5)]
    return {
        "recommended_id": "A",
        "proposals": [
            {"id": chr(65 + n), "label": f"Plan {n}", "direction": f"Direction {n}",
             "order_reason": "寻求到更新到回应", "tradeoffs": "中文版本待核对",
             "songs": [
                 {"song_id": str(i), "arrangement_id": str(i + 10),
                  "reason": "歌词支持主题", "position_reason": "承担这个阶段",
                  "theme_evidence": "歌词核心表达", "evidence_kind": "current_lyrics",
                  "tempo": "slow", "tempo_basis": "68 BPM, 4/4; not auditioned", "unknowns": []}
                 for i in orders[n]]
             }
            for n in range(count)
        ],
    }


def confirmed():
    state = prepared()
    e.propose(state, proposal())
    e.confirm(state, {"proposal_id": "A", "revision": state["revision"],
                      "confirmed": True, "user_message": "确认方案 A，按这个顺序下载"})
    return state


def resource(sid="attachment:1", kind=dl.SCORE, scope="selected", downloadable=True):
    return {"id": sid, "attachment_id": sid.split(":")[1], "filename": "原始简谱.pdf",
            "kind": kind, "scope": scope, "downloadable": downloadable}


class EngineTests(unittest.TestCase):
    def test_collects_requirements_then_preferences(self):
        state = e.new_state()
        e.requirements(state, {"theme": "跟随", "slots": ["fast", "slow", "any"]})
        self.assertEqual(state["stage"], "collect_preferences")
        e.requirements(state, {"preferences": []})
        self.assertEqual(state["stage"], "ready_to_search")

    def test_partial_requirements(self):
        state = e.new_state()
        e.requirements(state, {"theme": "呼求"})
        self.assertEqual(state["stage"], "collect_requirements")

    def test_one_two_three_plans_allowed(self):
        for count in (1, 2, 3):
            state = prepared()
            e.propose(state, proposal(count))
            self.assertEqual(len(state["proposals"]), count)

    def test_empty_or_four_plans_rejected(self):
        for plans in ([], proposal(3)["proposals"] + [proposal()["proposals"][0]]):
            with self.assertRaises(WorkflowError):
                e.propose(prepared(), {"proposals": plans})

    def test_no_duplicate_order_or_song(self):
        p = proposal(2)
        p["proposals"][1]["songs"] = copy.deepcopy(p["proposals"][0]["songs"])
        with self.assertRaises(WorkflowError):
            e.propose(prepared(), p)
        p = proposal()
        p["proposals"][0]["songs"][1] = copy.deepcopy(p["proposals"][0]["songs"][0])
        with self.assertRaises(WorkflowError):
            e.propose(prepared(), p)

    def test_exactly_three_and_real_ids(self):
        for invalid in ("missing", "two", "unknown_id"):
            p = proposal()
            if invalid == "missing":
                del p["proposals"][0]["songs"][0]["reason"]
            elif invalid == "two":
                p["proposals"][0]["songs"].pop()
            else:
                p["proposals"][0]["songs"][0]["arrangement_id"] = "999"
            with self.assertRaises(WorkflowError):
                e.propose(prepared(), p)

    def test_positional_tempo_conflict(self):
        p = proposal()
        p["proposals"][0]["songs"][0]["tempo"] = "fast"
        with self.assertRaises(WorkflowError):
            e.propose(prepared(), p)

    def test_preferences_not_mandatory(self):
        state = prepared()
        e.requirements(state, {"preferences": [{"title": "未选的歌", "song_id": "5"}]})
        e.propose(state, proposal())

    def test_mandatory_song_required(self):
        state = prepared()
        e.requirements(state, {"preferences": [{"title": "必选", "song_id": "5", "mode": "required"}]})
        with self.assertRaises(WorkflowError):
            e.propose(state, proposal())

    def test_unresolved_mandatory_needs_confirmation(self):
        state = prepared()
        e.requirements(state, {"preferences": [{"title": "同名", "mode": "required"}]})
        with self.assertRaises(WorkflowError):
            e.propose(state, proposal())

    def test_confirmation_and_order_guard(self):
        state = confirmed()
        self.assertEqual(e.confirmed_plan(state)["id"], "A")
        state["proposals"][0]["songs"].reverse()
        with self.assertRaises(WorkflowError):
            e.confirmed_plan(state)

    def test_stale_revision_and_inferred_confirmation_rejected(self):
        state = prepared()
        e.propose(state, proposal())
        for payload in (
            {"proposal_id": "A", "revision": 0, "confirmed": True, "user_message": "yes"},
            {"proposal_id": "A", "revision": state["revision"], "confirmed": True},
        ):
            with self.assertRaises(WorkflowError):
                e.confirm(state, payload)

    def test_feedback_invalidates(self):
        state = confirmed()
        e.feedback(state, {"message": "保留第三首，第一首换成快歌"})
        self.assertIsNone(state["confirmation"])
        self.assertTrue(state["history"])
        self.assertEqual(state["feedback"][0]["message"], "保留第三首，第一首换成快歌")
        with self.assertRaises(WorkflowError):
            e.confirmed_plan(state)

    def test_requirements_invalidate(self):
        state = confirmed()
        e.requirements(state, {"theme": "感恩"})
        with self.assertRaises(WorkflowError):
            e.confirmed_plan(state)

    def test_evidence_refresh_invalidates_changed_candidates_only(self):
        state = confirmed()
        candidate = copy.deepcopy(state["candidates"]["1:11"])
        for key in ("checked_at", "lyrics_available", "lyrics_sha256"):
            candidate.pop(key)
        e.add_candidates(state, [candidate | {"lyrics": LYRICS}])
        self.assertIsNotNone(state["confirmation"])
        e.add_candidates(state, [candidate | {"lyrics": LYRICS, "bpm": 90}])
        self.assertIsNone(state["confirmation"])

    def test_unknowns_need_explicit_acceptance(self):
        state = prepared()
        p = proposal()
        p["proposals"][0]["songs"][0].update(
            tempo="unknown", unknowns=["速度待核验"], evidence_kind="known_version"
        )
        e.propose(state, p)
        payload = {"proposal_id": "A", "revision": state["revision"],
                   "confirmed": True, "user_message": "接受未知项并确认"}
        with self.assertRaises(WorkflowError):
            e.confirm(state, payload)
        e.confirm(state, payload | {"accept_unknowns": True})

    def test_current_lyrics_cannot_be_fabricated(self):
        state = prepared()
        state["candidates"]["1:11"]["lyrics_available"] = False
        with self.assertRaises(WorkflowError):
            e.propose(state, proposal())

    def test_secret_fields_and_signed_urls_rejected(self):
        for data in ({"secret": "value"}, {"notes": "pco_pat_not_real"},
                     {"notes": "https://host/file?X-Amz-Signature=not_real"}):
            with self.assertRaises(WorkflowError):
                e.safe_input(data)

    def test_no_full_lyrics_persisted(self):
        self.assertNotIn(LYRICS, json.dumps(prepared(), ensure_ascii=False))

    def test_partial_input_validation(self):
        for payload in ({"slots": ["slow"]}, {"slots": ["slow", "medium", "slow"]},
                        {"preferences": "none"}, {"theme": ""}):
            with self.assertRaises(WorkflowError):
                e.requirements(prepared(), payload)


class SourceTests(unittest.TestCase):
    def setUp(self):
        # These tests isolate document behavior; bundle/audio delivery is tested in test_keys.
        def delivered(state, api, directory, save):
            for result in state["results"].values():
                result["audio"] = {"status": "downloaded"}
        patcher = patch("keys.deliver", side_effect=delivered)
        patcher.start()
        self.addCleanup(patcher.stop)
    def test_lyrics_priority_over_score(self):
        score = resource()
        lyrics = resource("attachment:2", dl.CHINESE_LYRICS)
        self.assertEqual(dl.choose([score, lyrics], {})["id"], "attachment:2")

    def test_field_priority(self):
        field = resource("lyrics:11", dl.CHINESE_LYRICS)
        self.assertEqual(dl.choose([resource("attachment:2", dl.CHINESE_LYRICS), field], {})["id"], "lyrics:11")

    def test_score_only_and_missing(self):
        self.assertEqual(dl.choose([resource()], {})["kind"], dl.SCORE)
        self.assertIsNone(dl.choose([], {}))
        self.assertIsNone(dl.choose([resource(kind="other")], {}))

    def test_ambiguous_documents_do_not_trigger_fallback(self):
        with self.assertRaises(WorkflowError) as caught:
            dl.choose([resource(), resource("attachment:2", "unknown")], {})
        self.assertEqual(caught.exception.code, "needs_review")

    def test_restricted_lyrics_not_replaced_by_score(self):
        with self.assertRaises(WorkflowError) as caught:
            dl.choose([resource(), resource("attachment:2", dl.CHINESE_LYRICS, downloadable=False)], {})
        self.assertEqual(caught.exception.code, "restricted")

    def test_different_version_and_multiple_scores_need_choice(self):
        for sources in ([resource(scope="other_arrangement")],
                        [resource(), resource("attachment:2")]):
            with self.assertRaises(WorkflowError):
                dl.choose(sources, {})

    def test_explicit_choice_respects_priority_and_source_changes(self):
        score, lyrics = resource(), resource("attachment:2", dl.CHINESE_LYRICS)
        review = {"selected_source_id": score["id"], "choice_fingerprint": dl.source_fingerprint(score)}
        with self.assertRaises(WorkflowError):
            dl.choose([score, lyrics], review)
        chosen = resource(scope="other_arrangement")
        review = {"selected_source_id": chosen["id"], "choice_fingerprint": dl.source_fingerprint(chosen)}
        self.assertEqual(dl.choose([chosen], review)["id"], chosen["id"])
        with self.assertRaises(WorkflowError):
            dl.choose([chosen | {"updated_at": "changed"}], review)

    def test_review_unknown_then_score_fallback(self):
        unknown = resource("attachment:2", "unknown")
        review = {"classifications": {unknown["id"]: {
            "kind": "other", "fingerprint": dl.source_fingerprint(unknown), "evidence": "English chords"
        }}}
        self.assertEqual(dl.choose([resource(), unknown], review)["kind"], dl.SCORE)

    def test_name_classification_conservative(self):
        for filename, kind in (
            ("中文歌词.docx", dl.CHINESE_LYRICS), ("如你-簡譜.pdf", dl.SCORE),
            ("Chinese lyrics.pdf", dl.CHINESE_LYRICS), ("English lyrics.pdf", "other"),
            ("歌名.pdf", "unknown"), ("chords.pdf", "other"),
        ):
            self.assertEqual(dl.filename_kind(filename), kind)

    def test_chinese_title_not_chinese_lyrics(self):
        self.assertFalse(dl.chinese("神的良善\nI love you Lord\nYour mercy never fails"))
        self.assertTrue(dl.chinese(LYRICS))

    def test_original_txt_pdf_and_invalid_file(self):
        dl.validate_file(LYRICS.encode(), "歌词.txt", dl.CHINESE_LYRICS)
        dl.validate_file(PDF, "简谱.pdf", dl.SCORE)
        for data, name in ((b"<html>login</html>", "lyrics.pdf"), (b'{"error":true}', "lyrics.docx"),
                           (b"English only", "歌词.txt"), (b"PKbad", "歌词.docx"),
                           (b"%PDF-truncated", "简谱.pdf")):
            with self.assertRaises(WorkflowError):
                dl.validate_file(data, name, dl.CHINESE_LYRICS)

    def test_docx_is_validated_without_transcription(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml",
                            f'<w:document xmlns:w="urn:w"><w:t>{LYRICS}</w:t></w:document>')
        dl.validate_file(output.getvalue(), "中文歌词.docx", dl.CHINESE_LYRICS)

    def test_no_overwrite_or_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            existing = folder / "原始简谱.pdf"
            existing.write_bytes(b"user file")
            path = dl.save_original(folder, existing.name, PDF)
            self.assertEqual(existing.read_bytes(), b"user file")
            self.assertNotEqual(existing, path)
            self.assertEqual(dl.save_original(folder, existing.name, PDF), path)
            escaped = dl.save_original(folder, "../outside.pdf", PDF)
            self.assertEqual(escaped.parent, folder.resolve())

    def test_no_download_without_confirmation(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(dl, "inventory") as inv:
            with self.assertRaises(WorkflowError):
                dl.download(prepared(), object(), temp)
            inv.assert_not_called()
            self.assertFalse(list(Path(temp).iterdir()))

    def test_partial_results_and_retry_only_failed_files(self):
        state = test_support.keyed(confirmed())
        field = resource("lyrics:11", dl.CHINESE_LYRICS) | {
            "text": LYRICS, "filename": "中文歌词.txt"
        }
        calls = []

        def inventory(api, song):
            calls.append(song["song_id"])
            if song["song_id"] == "1":
                return [field]
            if song["song_id"] == "2":
                return []
            raise WorkflowError("network_error", "Not verified")

        with tempfile.TemporaryDirectory() as temp, patch.object(dl, "inventory", side_effect=inventory):
            dl.download(state, object(), temp)
            self.assertEqual(state["stage"], "partial")
            self.assertEqual(state["results"]["1"]["status"], "downloaded")
            self.assertEqual(state["results"]["2"]["status"], "missing")
            self.assertEqual(state["results"]["3"]["status"], "network_error")
            calls.clear()
            dl.download(state, object(), temp)
            self.assertEqual(calls, ["2", "3"])
            self.assertEqual(len(list(Path(temp).iterdir())), 1)

    def test_auth_error_stops_remaining_requests(self):
        state = test_support.keyed(confirmed())
        with tempfile.TemporaryDirectory() as temp, patch.object(
            dl, "inventory", side_effect=WorkflowError("auth_error", "Denied")
        ) as inv:
            dl.download(state, object(), temp)
            self.assertEqual(inv.call_count, 1)
            self.assertEqual(state["results"]["1"]["status"], "auth_error")
            self.assertFalse(list(Path(temp).iterdir()))

    def test_missing_creates_no_song_files(self):
        state = test_support.keyed(confirmed())
        with tempfile.TemporaryDirectory() as temp, patch.object(dl, "inventory", return_value=[]):
            dl.download(state, object(), Path(temp) / "downloads")
            self.assertEqual(state["stage"], "completed")
            self.assertFalse((Path(temp) / "downloads").exists())


class APITests(unittest.TestCase):
    def test_permission_errors_not_retried_or_reported_missing(self):
        for code in (401, 403):
            api = PCO()
            api._auth = "test-only"
            with patch.object(api._api, "open", side_effect=urllib.error.HTTPError(
                "https://api.planningcenteronline.com/services/v2/songs", code, "denied", {}, None
            )) as request:
                with self.assertRaises(WorkflowError) as error:
                    api.request("/services/v2/songs")
                self.assertEqual(error.exception.code, "auth_error")
                self.assertEqual(request.call_count, 1)

    def test_timeout_retried_once_and_long_cooldown_not_shortened(self):
        api = PCO()
        api._auth = "test-only"
        with patch.object(api._api, "open", side_effect=TimeoutError), patch("pco_api.time.sleep") as sleep:
            with self.assertRaises(WorkflowError) as error:
                api.request("/services/v2/songs")
            self.assertEqual(error.exception.code, "network_error")
            self.assertEqual(sleep.call_count, 1)
        with patch.object(api._api, "open", side_effect=urllib.error.HTTPError(
            "https://api.planningcenteronline.com/services/v2/songs", 429, "limit",
            {"Retry-After": "120"}, None
        )) as request, patch("pco_api.time.sleep") as sleep:
            with self.assertRaises(WorkflowError) as error:
                api.request("/services/v2/songs")
            self.assertEqual(error.exception.code, "rate_limited")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_signed_file_request_has_no_api_authorization(self):
        api = PCO()
        api._auth = "test-only"
        with patch.object(api, "request", return_value={
            "data": {"attributes": {"attachment_url": "https://files.example/score.pdf?signature=test"}}
        }) as opened, patch("pco_api.public_https", side_effect=lambda url: url), patch.object(
            api._files, "open", return_value=io.BytesIO(PDF)
        ) as transfer:
            self.assertEqual(api.file("1"), PDF)
            self.assertEqual(opened.call_args.kwargs["method"], "POST")
            self.assertIsNone(transfer.call_args.args[0].get_header("Authorization"))

    def test_pagination_and_cycles(self):
        api = PCO()
        with patch.object(api, "request", side_effect=[
            {"data": [{"id": "1"}], "links": {"next": "/services/v2/songs?page=2"}},
            {"data": [{"id": "2"}]},
        ]):
            self.assertEqual([r["id"] for r in api.pages("/services/v2/songs")], ["1", "2"])
        with patch.object(api, "request", return_value={
            "data": [], "links": {"next": "/services/v2/songs"}
        }):
            with self.assertRaises(WorkflowError):
                api.pages("/services/v2/songs")

    def test_no_other_api_origin_or_mutation(self):
        api = PCO()
        for path, method in (("https://evil.example/services/v2/songs", "GET"),
                             ("/services/v2/songs/1", "POST"), ("/people/v2/me", "GET")):
            with self.assertRaises(WorkflowError):
                api.request(path, method)

    def test_authenticated_redirect_refused(self):
        with self.assertRaises(WorkflowError):
            pco_api.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_local_or_plain_http_download_url_refused(self):
        for url in ("http://example.com/a", "https://user:pass@example.com/a"):
            with self.assertRaises(WorkflowError):
                pco_api.public_https(url)
        with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            with self.assertRaises(WorkflowError):
                pco_api.public_https("https://example.com/a")

    def test_inventory_reads_song_all_active_arrangements_and_keys(self):
        api = PCO()
        song = {"song_id": "1", "arrangement_id": "11", "title": "Song", "bpm": 68, "meter": "4/4"}
        arrangement = {"id": "11", "attributes": {"bpm": 68, "meter": "4/4", "lyrics": LYRICS}}
        other = {"id": "12", "attributes": {"lyrics": ""}}
        archived = {"id": "13", "attributes": {"archived_at": "past"}}
        paths = []

        def pages(path):
            paths.append(path)
            if path.endswith("/arrangements?per_page=100"):
                return [arrangement, other, archived]
            if path.endswith("/11/keys"):
                return [{"id": "100", "links": {"attachments": "/services/v2/key-attachments"}}]
            return []

        with patch.object(api, "request", return_value={"data": {"attributes": {}}}), patch.object(api, "pages", side_effect=pages):
            sources = dl.inventory(api, song)
            self.assertEqual(sources[0]["text"], LYRICS)
            self.assertIn("/services/v2/songs/1/attachments", paths)
            self.assertIn("/services/v2/songs/1/arrangements/12/attachments", paths)
            self.assertIn("/services/v2/key-attachments", paths)
            self.assertFalse(any("/13/" in p for p in paths))
            self.assertNotIn(LYRICS, json.dumps(dl.public_inventory(sources), ensure_ascii=False))

    def test_arrangement_change_requires_review(self):
        api = PCO()
        song = {"song_id": "1", "arrangement_id": "11", "title": "Song", "bpm": 68, "meter": "4/4"}
        with patch.object(api, "request", return_value={"data": {"attributes": {}}}), patch.object(
            api, "pages", return_value=[{"id": "11", "attributes": {"bpm": 90, "meter": "4/4"}}]
        ):
            with self.assertRaises(WorkflowError):
                dl.inventory(api, song)

    def test_short_chinese_field_is_pending_not_missing(self):
        api = PCO()
        song = {"song_id": "1", "arrangement_id": "11", "title": "Song", "bpm": 68, "meter": "4/4"}
        def pages(path):
            return [{"id": "11", "attributes": {
                "bpm": 68, "meter": "4/4", "lyrics": "主啊"
            }}] if path.endswith("/arrangements?per_page=100") else []
        with patch.object(api, "request", return_value={"data": {"attributes": {}}}), patch.object(
            api, "pages", side_effect=pages
        ):
            sources = dl.inventory(api, song)
            self.assertEqual(sources[0]["kind"], "unknown")
            with self.assertRaises(WorkflowError) as error:
                dl.choose(sources, {})
            self.assertEqual(error.exception.code, "needs_review")


class CLITests(unittest.TestCase):
    def call_cli(self, args, payload=None):
        output = io.StringIO()
        with patch.object(sys, "argv", ["workflow.py", *args]), patch.object(
            sys, "stdin", io.StringIO(json.dumps(payload or {}, ensure_ascii=False))
        ), patch.object(sys, "stdout", output):
            workflow.main()
        return json.loads(output.getvalue())

    def test_full_persistent_cli_flow_with_simulated_pco(self):
        api = PCO()
        rows = [
            {"song_id": str(i), "arrangement_id": str(i + 10), "title": f"Song {i}",
             "bpm": 68, "meter": "4/4", "lyrics": LYRICS}
            for i in range(1, 6)
        ]
        field = resource("lyrics:11", dl.CHINESE_LYRICS) | {
            "text": LYRICS, "filename": "中文歌词.txt"
        }
        def sources(api, song):
            return [field] if song["song_id"] == "1" else [resource()] if song["song_id"] == "2" else []
        with tempfile.TemporaryDirectory() as temp, patch(
            "workflow.PCO", return_value=api
        ), patch.object(api, "inspect", side_effect=lambda sid: [r for r in rows if r["song_id"] == sid]), patch.object(
            dl, "inventory", side_effect=sources
        ), patch.object(api, "file", return_value=PDF) as transfer:
            created = self.call_cli(["new", "--runs-dir", temp])
            args = ["--state", created["state_path"]]
            self.call_cli(args + ["requirements"], {
                "theme": "从呼求到更新再到跟随", "slots": ["slow"] * 3, "preferences": []
            })
            self.call_cli(args + ["inspect", "1", "2", "3", "4", "5"])
            first = self.call_cli(args + ["propose"], proposal(3))
            self.call_cli(args + ["feedback"], {"message": "希望更集中，只保留一套"})
            second = self.call_cli(args + ["propose"], proposal(1))
            self.assertGreater(second["revision"], first["revision"])
            self.call_cli(args + ["confirm"], {
                "proposal_id": "A", "revision": second["revision"], "confirmed": True,
                "user_message": "确认这一套，按这个顺序下载",
            })
            state_path = Path(created["state_path"])
            state = json.loads(state_path.read_text())
            test_support.approve(state, Path(temp) / "audition")
            workflow.persist(state_path, state)
            with patch("keys.inventory", side_effect=test_support.inventory), patch("keys.validate_mp3"):
                results = self.call_cli(args + ["download"])
            self.assertEqual(results["1"]["kind"], dl.CHINESE_LYRICS)
            self.assertEqual(Path(results["1"]["path"]).read_text(), LYRICS)
            self.assertEqual(results["2"]["kind"], dl.SCORE)
            self.assertEqual(results["3"]["status"], "missing")
            with patch("keys.inventory", side_effect=test_support.inventory), patch("keys.validate_mp3"):
                self.call_cli(args + ["download"])
            self.assertEqual(transfer.call_count, 1)
            final = self.call_cli(args + ["show"])
            self.assertEqual(final["stage"], "completed")
            self.assertEqual(len(list((Path(created["state_path"]).parent / "downloads").iterdir())), 5)

    def test_real_cli_session_persists_and_rejects_early_download(self):
        script = str(Path(__file__).with_name("workflow.py"))
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, script, "new", "--runs-dir", temp],
                                    capture_output=True, text=True, check=True)
            state_path = json.loads(result.stdout)["state_path"]
            result = subprocess.run([sys.executable, script, "--state", state_path, "requirements"],
                                    input=json.dumps({"theme": "呼求", "slots": ["slow"] * 3}),
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["stage"], "collect_preferences")
            result = subprocess.run([sys.executable, script, "--state", state_path, "download"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("confirmation", json.loads(result.stdout)["message"])
            self.assertFalse((Path(state_path).parent / "downloads").exists())


if __name__ == "__main__":
    unittest.main()
