"""Quality gates run against isolated sessions; no live PCO/approval claims."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from docx import Document
from lxml import etree as ET

import artifacts
from pco_api import WorkflowError
import setlist as sl
import spoken
import status
import test_setlist as fixtures
from test_spoken import draft, song_state
from test_support import visual_fixture
import workflow
import live_scenario


class TimingWindowTests(unittest.TestCase):
    def settings(self, **window_updates):
        slots = spoken.default_slots()
        slots[-1]["window"] = {
            "bars": 8, "beats_per_bar": 4, "bpm": 70, "reserve_seconds": 5,
            "basis": "Isolated 8-bar/quarter-note-tempo fixture, not live evidence",
            "tempo_basis": "reference_only",
        } | window_updates
        return {"slots": slots}

    def test_35_seconds_rejected_20_seconds_accepted_in_eight_bars(self):
        state = song_state()
        spoken.requirements(state, self.settings())
        payload = draft()
        payload["cues"][-1]["estimated_seconds"] = 35
        with self.assertRaisesRegex(WorkflowError, "exceeds the interlude"):
            spoken.propose(state, payload)
        payload["cues"][-1]["estimated_seconds"] = 20
        result = spoken.propose(state, payload)
        timing = result["draft"]["cues"][-1]["timing"]
        self.assertAlmostEqual(timing["available_seconds"], 8 * 4 * 60 / 70 - 5)
        self.assertEqual(timing["status"], "estimated_not_rehearsed")

    def test_invalid_window_numbers_and_unstated_beat_unit_rejected(self):
        for updates in ({"bars": True}, {"bars": 0}, {"bpm": float("nan")},
                        {"bpm": float("inf")}, {"beats_per_bar": 0},
                        {"reserve_seconds": -1}, {"reserve_seconds": 100},
                        {"tempo_basis": "rehearsed_automatically"}, {"basis": ""}):
            with self.subTest(updates=updates), self.assertRaises(WorkflowError):
                spoken.requirements(song_state(), self.settings(**updates))
        settings = self.settings()
        del settings["slots"][-1]["window"]["beats_per_bar"]
        with self.assertRaises(WorkflowError):
            spoken.requirements(song_state(), settings)

    def test_window_not_allowed_before_intro_and_target_still_applies(self):
        settings = self.settings()
        settings["slots"][1]["window"] = settings["slots"][-1].pop("window")
        with self.assertRaises(WorkflowError):
            spoken.requirements(song_state(), settings)
        state = song_state()
        settings = self.settings()
        settings["slots"][-1]["target_seconds"] = 15
        spoken.requirements(state, settings)
        with self.assertRaises(WorkflowError):
            spoken.propose(state, draft())

    def test_compound_meter_uses_explicit_tempo_beat_not_meter_numerator(self):
        settings = self.settings(beats_per_bar=2, bpm=60, reserve_seconds=2)
        self.assertEqual(spoken.window_seconds(settings["slots"][-1]), 14)

    def test_live_scenario_inputs_bind_stored_bars_and_do_not_invent_meter_tempo(self):
        context = {"songs": [
            {"material_fingerprint": "fixture", "interludes": [
                {"route_index": 0, "bars": 4}, {"route_index": 7, "bars": 8}]} for _ in (1, 2, 3)]}
        slots = live_scenario.speaking_slots(context, [{"meter": "4/4", "bpm": 70}] * 3)
        self.assertEqual(slots[-1]["route_index"], 7)
        self.assertEqual(slots[-1]["window"]["tempo_basis"], "reference_only")
        self.assertGreater(spoken.window_seconds(slots[-1]), 20)
        for song in ({"meter": "6/8", "bpm": 70}, {"meter": "4/4", "bpm": None}):
            unknown = live_scenario.speaking_slots(context, [song] * 3)[-1]
            self.assertNotIn("window", unknown)
            self.assertIn("timing_unknown_reason", unknown)


class QualityGateTests(unittest.TestCase):
    setUp = fixtures.SetlistTests.setUp
    ready = fixtures.SetlistTests.ready
    downloaded_material = fixtures.SetlistTests.downloaded_material

    def export(self, name="quality.docx"):
        sl.propose(self.state, self.ready())
        return sl.export(self.state, self.root / name, draft=True)

    def review_payload(self, exported):
        return {"path": exported["path"], "sha256": exported["sha256"],
                "confirmed": True, "user_message": "AUTOMATION ONLY simulated native review",
                "checks": ["layout", "colors", "content", "pagination"],
                "visual": visual_fixture()}

    def stored_bars(self):
        entry = self.state["materials"]["3"]
        r = sl.record(entry["database"], entry["id"])
        r["route"] = [{"label": "IN", "cue": "4"}, {"label": "V1", "cue": ""},
                      {"label": "C", "cue": ""}, {"label": "Int", "cue": "8"},
                      {"label": "B", "cue": ""}]
        r.pop("citation", None)
        with sl.rag.connect(entry["database"]) as db:
            db.execute("UPDATE songs SET payload=? WHERE id=?", (sl.rag.dump(r), r["id"]))
        sl.prepare(self.state, {"song_id": "3", "id": r["id"],
                               "version_basis": "Isolated two-interlude fixture"}, self.root, self.base)
        return self.state["materials"]["3"]

    def test_known_interlude_requires_exact_occurrence_and_material_binding(self):
        self.ready()
        entry = self.stored_bars()
        context = sl.spoken_context(self.state)
        occurrences = context["songs"][2]["interludes"]
        self.assertEqual([(i["route_index"], i["bars"]) for i in occurrences], [(0, 4), (3, 8)])
        spoken.requirements(self.state, {})
        with self.assertRaisesRegex(WorkflowError, "Bind the speaking interlude"):
            spoken.propose(self.state, draft())
        slots = spoken.default_slots()
        slots[-1].update(
            route_index=3, material_fingerprint=entry["fingerprint"],
            window={"bars": 8, "beats_per_bar": 4, "bpm": 70, "reserve_seconds": 5,
                    "basis": "Isolated reference fixture", "tempo_basis": "reference_only"})
        spoken.requirements(self.state, {"slots": slots})
        payload = draft()
        payload["cues"][-1]["estimated_seconds"] = 20
        spoken.propose(self.state, payload)
        slots[-1]["route_index"] = 0
        spoken.requirements(self.state, {"slots": slots})
        with self.assertRaisesRegex(WorkflowError, "stored bars"):
            spoken.propose(self.state, payload)
        slots[-1]["route_index"] = 1
        spoken.requirements(self.state, {"slots": slots})
        with self.assertRaisesRegex(WorkflowError, "instrumental occurrence"):
            spoken.propose(self.state, payload)

    def test_unknown_tempo_must_be_explained_and_disclosed(self):
        self.ready()
        entry = self.stored_bars()
        slots = spoken.default_slots()
        slots[-1].update(route_index=3, material_fingerprint=entry["fingerprint"],
                         timing_unknown_reason="Fixture tempo remains unknown")
        spoken.requirements(self.state, {"slots": slots})
        payload = draft()
        with self.assertRaisesRegex(WorkflowError, "Disclose"):
            spoken.propose(self.state, payload)
        payload["cues"][-1]["unknowns"] = ["Fixture tempo remains unknown"]
        spoken.propose(self.state, payload)
        with self.assertRaises(WorkflowError):
            spoken.confirm(self.state, {"revision": self.state["spoken"]["revision"],
                                        "confirmed": True, "user_message": "AUTOMATION ONLY"})

    def test_stale_material_and_current_route_cannot_reuse_old_window(self):
        payload = self.ready()
        entry = self.stored_bars()
        slots = spoken.default_slots()
        slots[-1].update(route_index=3, material_fingerprint=entry["fingerprint"],
                         window={"bars": 8, "beats_per_bar": 4, "bpm": 70, "reserve_seconds": 5,
                                 "basis": "Isolated fixture", "tempo_basis": "reference_only"})
        spoken.requirements(self.state, {"slots": slots})
        speech = draft()
        speech["cues"][-1]["estimated_seconds"] = 20
        spoken.propose(self.state, speech)
        spoken.confirm(self.state, {"revision": self.state["spoken"]["revision"],
                                    "confirmed": True, "user_message": "AUTOMATION ONLY"})
        r = sl.material(self.state, "3")[0]
        payload["main"][2].update(route=copy.deepcopy(r["route"]), structure_basis="fixture route")
        sl.propose(self.state, payload)
        payload["main"][2]["route"][3]["current_cue"] = "4 话语"
        with self.assertRaisesRegex(WorkflowError, "bars disagree"):
            sl.propose(self.state, payload)
        slots[-1]["material_fingerprint"] = "stale"
        spoken.requirements(self.state, {"slots": slots})
        with self.assertRaisesRegex(WorkflowError, "fingerprint"):
            spoken.propose(self.state, speech)

    def test_version_comparison_reports_path_lyrics_bars_without_auto_selecting(self):
        self.ready()
        entry = self.stored_bars()
        r = sl.record(entry["database"], entry["id"])
        r.update(id="alternate-3", source_name="isolated ambiguous version", issues=["unresolved V"])
        r.pop("citation", None)
        with sl.rag.connect(entry["database"]) as db:
            db.execute("INSERT INTO songs VALUES (?,?,?,?,?,?)",
                       (r["id"], r["source_id"], r["title"], "main", sl.rag.dump(r), sl.rag.vector(r["title"])))
        result = sl.rag.resolve(entry["database"], "如你")
        self.assertEqual(result["status"], "choose_version")
        self.assertEqual(result["recommended_id"], entry["id"])
        self.assertEqual(len(result["candidates"]), 2)
        for candidate in result["candidates"]:
            self.assertEqual(candidate["lyric_labels"], ["V1", "C", "B"])
            self.assertEqual([i["bars"] for i in candidate["interludes"]], [4, 8])
            self.assertEqual(len(candidate["route"]), 5)
        r["issues"] = []
        with sl.rag.connect(entry["database"]) as db:
            db.execute("UPDATE songs SET payload=? WHERE id=?", (sl.rag.dump(r), r["id"]))
        self.assertIsNone(sl.rag.resolve(entry["database"], "如你")["recommended_id"])
        self.assertEqual(self.state["materials"]["3"], entry)

    def test_metadata_hidden_by_default_but_retained_in_sources(self):
        payload = self.ready()
        payload["event"] = {"title": "Draft heading", "date": "2026-12-13",
                            "personnel": "Unassigned singers", "basis": "Isolated input"}
        sl.propose(self.state, payload)
        result = sl.export(self.state, self.root / "metadata-hidden.docx", draft=True)
        text = "\n".join(p.text for p in Document(result["path"]).paragraphs)
        self.assertNotIn("Draft heading", text)
        self.assertNotIn("Unassigned singers", text)
        self.assertEqual(json.loads(Path(result["sources"]).read_text())["selection"]["event"], payload["event"])
        with self.assertRaises(WorkflowError):
            sl.propose(self.state, payload | {"presentation": {"show_event_metadata": "false"}})

    def test_exact_export_reuse_preserves_files_and_modes(self):
        original = self.export()
        result = sl.export(self.state, self.root / "redundant.docx", draft=True, reuse=True)
        self.assertTrue(result["reused"])
        self.assertEqual(result["path"], original["path"])
        self.assertFalse((self.root / "redundant.docx").exists())
        self.assertEqual(len(self.state["setlist"]["exports"]), 1)
        sl.confirm(self.state, {"revision": self.state["setlist"]["revision"], "confirmed": True,
                                "user_message": "AUTOMATION ONLY final approval"})
        formal = sl.export(self.state, self.root / "formal.docx", reuse=True)
        self.assertNotEqual(formal["path"], original["path"])

    def test_reuse_never_accepts_modified_document_or_sources(self):
        original = self.export()
        Path(original["sources"]).write_text("user edit")
        fresh = sl.export(self.state, self.root / "fresh.docx", draft=True, reuse=True)
        self.assertNotEqual(original["path"], fresh["path"])
        self.assertEqual(Path(original["sources"]).read_text(), "user edit")
        Path(fresh["path"]).write_bytes(b"user edited document")
        newer = sl.export(self.state, self.root / "newer.docx", draft=True, reuse=True)
        self.assertNotEqual(newer["path"], fresh["path"])

    def test_generic_review_and_missing_page_or_baseline_rejected(self):
        result = self.export()
        good = self.review_payload(result)
        for change in ("generic", "missing_page", "duplicate_page", "missing_baseline", "renderer"):
            payload = copy.deepcopy(good)
            if change == "generic":
                del payload["visual"]
            elif change == "missing_page":
                payload["visual"]["pages"].pop()
            elif change == "duplicate_page":
                payload["visual"]["pages"][1]["page"] = 1
            elif change == "missing_baseline":
                payload["visual"]["songs"][0]["checks"].remove("first_lyric_baseline")
            else:
                payload["visual"]["app"] = "font-missing auxiliary renderer"
            with self.subTest(change=change), self.assertRaises(WorkflowError):
                artifacts.review(self.state, payload)
        review = artifacts.review(self.state, good)
        self.assertEqual(review["review_version"], 5)
        self.assertEqual(review["structural"]["scope"], "ooxml_invariants_not_visual_acceptance")

    def test_structurally_broken_export_cannot_receive_review(self):
        result = self.export()
        path = Path(result["path"])
        with zipfile.ZipFile(path) as archive:
            parts = {n: archive.read(n) for n in archive.namelist()}
        root = ET.fromstring(parts["word/document.xml"])
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        height = root.find(".//w:tbl/w:tr/w:trPr/w:trHeight", ns)
        height.set("{%s}val" % ns["w"], "900")
        parts["word/document.xml"] = ET.tostring(root)
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        # Registering the corrupt hash is a fixture for a buggy exporter, not a tamper bypass.
        result["sha256"] = sl.rag.sha(path)
        next(r for r in self.state["artifacts"] if r["path"] == str(path))["sha256"] = result["sha256"]
        with self.assertRaisesRegex(ValueError, "equal exact heights"):
            artifacts.review(self.state, self.review_payload(result))

    def test_status_lists_remaining_files_without_mutation(self):
        result = self.export()
        artifacts.review(self.state, self.review_payload(result))
        second = sl.export(self.state, self.root / "second.docx", draft=True)
        artifacts.review(self.state, self.review_payload(second))
        before = copy.deepcopy(self.state)
        summary = status.summary(self.state)
        self.assertEqual(self.state, before)
        self.assertEqual(summary["current_artifact"]["path"], second["path"])
        self.assertEqual(summary["cleanup_remaining"], [{
            "path": result["path"], "action": "check_open_or_modified_then_retire"}])
        self.assertFalse(summary["delivery_complete"])
        artifacts.retire(self.state, {"path": result["path"], "sha256": result["sha256"],
                                     "replacement": second["path"], "confirmed": True,
                                     "user_message": "AUTOMATION ONLY obsolete fixture"})
        artifacts.cleanup(self.state, {"apply": True})
        self.assertEqual(status.summary(self.state)["cleanup_remaining"], [])
        self.assertTrue(self.source.exists())

    def test_status_does_not_treat_legacy_review_as_new_native_evidence(self):
        result = self.export()
        self.state["artifact_reviews"] = {result["path"]: {
            "sha256": result["sha256"], "checks": ["layout", "colors", "content", "pagination"]}}
        summary = status.summary(self.state)
        self.assertFalse(summary["visual_review_valid"])
        self.assertEqual(summary["visual_review_format"], 1)
        self.assertNotIn("visual", self.state["artifact_reviews"][result["path"]])

    def test_delivery_status_requires_current_formal_approval_and_no_modified_bytes(self):
        sl.propose(self.state, self.ready())
        sl.confirm(self.state, {"revision": self.state["setlist"]["revision"], "confirmed": True,
                                "user_message": "AUTOMATION ONLY final approval"})
        # This local fixture exercises the non-test status branch, never a production session.
        self.state["test_run"] = False
        result = sl.export(self.state, self.root / "formal.docx")
        artifacts.review(self.state, self.review_payload(result))
        self.assertFalse(status.summary(self.state)["delivery_complete"])
        artifacts.finalize(self.state, self.review_payload(result) | {
            "cleanup": {"checked_files": [], "user_message": "AUTOMATION ONLY no old files"}})
        self.assertTrue(status.summary(self.state)["delivery_complete"])
        self.state["test_run"] = True
        self.assertFalse(status.summary(self.state)["delivery_complete"])
        self.state["test_run"] = False
        Path(result["path"]).write_bytes(b"modified")
        summary = status.summary(self.state)
        self.assertFalse(summary["delivery_complete"])
        self.assertFalse(summary["artifact_bytes_valid"])

    def test_read_only_cli_status_and_spoken_context_do_not_change_state(self):
        self.export()
        path = self.root / "state.json"
        workflow.persist(path, self.state)
        before = path.read_bytes()
        for command in ("status", "spoken-context"):
            output = io.StringIO()
            with patch.object(sys, "argv", ["workflow.py", "--state", str(path), command]), \
                    patch.object(sys, "stdout", output), patch.object(workflow, "PCO") as api:
                workflow.main()
            self.assertEqual(path.read_bytes(), before)
            self.assertIsInstance(json.loads(output.getvalue()), dict)
            api.return_value.request.assert_not_called()

    def test_cli_quality_roundtrip_and_rejected_review_does_not_persist(self):
        payload = self.ready()
        path = self.root / "state.json"
        workflow.persist(path, self.state)

        def call(command, data=None, extra=(), code=0):
            process = subprocess.run(
                [sys.executable, "-B", str(Path(workflow.__file__)),
                 "--state", str(path), command, *extra],
                input=json.dumps(data) if data is not None else None,
                capture_output=True, text=True, check=False)
            self.assertEqual(process.returncode, code, process.stdout + process.stderr)
            return json.loads(process.stdout)

        call("setlist-propose", payload)
        result = call("setlist-export", extra=("--draft", "--output", str(self.root / "cli.docx")))
        reused = call("setlist-export", extra=("--draft", "--reuse-existing",
                                               "--output", str(self.root / "unused.docx")))
        self.assertEqual(result["path"], reused["path"])
        self.assertTrue(reused["reused"])
        review = self.review_payload(result)
        bad = copy.deepcopy(review)
        del bad["visual"]
        before = path.read_bytes()
        call("artifact-review", bad, code=2)
        self.assertEqual(path.read_bytes(), before)
        call("artifact-review", review)
        current = call("status")
        self.assertTrue(current["visual_review_valid"])
        self.assertEqual(current["visual_review_format"], 5)
        self.assertFalse(current["delivery_complete"])
        self.assertEqual(current["cleanup_remaining"], [])
        self.assertEqual(len(call("spoken-context")["songs"]), 3)

    def test_new_export_cannot_bypass_full_style_acceptance_with_old_review(self):
        result = self.export()
        self.assertEqual(result["required_review_version"], 5)
        self.state["artifact_reviews"] = {result["path"]: {
            "sha256": result["sha256"], "review_version": 4}}
        self.assertFalse(status.summary(self.state)["visual_review_valid"])
        temp = self.root / "preview.png"
        temp.write_bytes(b"isolated preview")
        artifacts.register(self.state, temp, "temporary", result["path"])
        with self.assertRaises(WorkflowError):
            artifacts.cleanup(self.state, {"apply": True})
        self.assertTrue(temp.exists())

    def test_mixed_songs_shared_third_response_and_separate_ending_rejected(self):
        result = self.export()
        for change in ("mixed", "third_response", "ending_own_page", "no_blocks", "no_style_check",
                       "no_response_styles"):
            payload = self.review_payload(result)
            if change == "mixed":
                payload["visual"]["pages"][1]["blocks"] = ["main:1", "main:2"]
            elif change == "third_response":
                payload["visual"]["pages"][2]["blocks"] = ["main:3", "response", "ending"]
            elif change == "ending_own_page":
                payload["visual"]["pages"][3]["blocks"] = ["response"]
            elif change == "no_blocks":
                del payload["visual"]["pages"][0]["blocks"]
            elif change == "no_style_check":
                payload["visual"]["songs"][0]["checks"].remove("lyric_spacing")
            else:
                del payload["visual"]["response_ending"]
            with self.subTest(change=change), self.assertRaises(WorkflowError):
                artifacts.review(self.state, payload)

    def test_unavoidable_lyric_split_requires_recorded_layout_attempts(self):
        result = self.export()
        payload = self.review_payload(result)
        visual = payload["visual"]
        visual["page_count"] = 5
        extra = copy.deepcopy(visual["pages"][0])
        visual["pages"].insert(1, extra)
        for page, number in zip(visual["pages"], range(1, 6)):
            page["page"] = number
        visual["songs"][0].update(pages=[1, 2], lyrics_pages=[1, 2])
        visual["songs"][1].update(pages=[3], lyrics_pages=[3])
        visual["songs"][2].update(pages=[4], lyrics_pages=[4])
        visual["response_ending"]["page"] = 5
        with self.assertRaisesRegex(WorkflowError, "unavoidable splits"):
            artifacts.review(self.state, payload)
        visual["songs"][0]["pagination_exception"] = {
            "reason": "AUTOMATION ONLY long song fixture, not an actual visual observation",
            "adjustments": ["Simulated column/spacing check; did not shrink fonts or remove lyrics"]}
        artifacts.review(self.state, payload)
        visual["songs"][0]["lyrics_pages"] = [2]
        with self.assertRaisesRegex(WorkflowError, "orphan title"):
            artifacts.review(self.state, payload)

    def test_alignment_gap_and_width_observations_are_mandatory(self):
        result = self.export()
        for check in ("left_edges", "table_lyric_gap", "table_width_balance", "section_spacing"):
            payload = self.review_payload(result)
            payload["visual"]["songs"][1]["checks"].remove(check)
            with self.subTest(check=check), self.assertRaises(WorkflowError):
                artifacts.review(self.state, payload)
        payload = self.review_payload(result)
        payload["visual"]["response_ending"]["checks"].remove("left_edges")
        with self.assertRaises(WorkflowError):
            artifacts.review(self.state, payload)

    def test_main_and_response_ending_page_boundaries_are_forced_in_ooxml(self):
        result = self.export()
        with zipfile.ZipFile(result["path"]) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = root.findall("w:body/w:p", ns)
        titles = [(p, "".join(p.xpath(".//w:t/text()", namespaces=ns))) for p in paragraphs]
        main = [(p, text) for p, text in titles if text.startswith(("1. ", "2. ", "3. "))]
        self.assertEqual(len(main), 3)
        for p, _ in main[1:]:
            self.assertIsNotNone(p.find("w:pPr/w:pageBreakBefore", ns))
        response = next(p for p, text in titles if text.startswith("回应诗歌："))
        ending = next(p for p, text in titles if text.startswith("结束诗歌："))
        self.assertIsNotNone(response.find("w:pPr/w:pageBreakBefore", ns))
        self.assertIsNone(ending.find("w:pPr/w:pageBreakBefore", ns))


if __name__ == "__main__":
    unittest.main()
