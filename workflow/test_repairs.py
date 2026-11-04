"""Regression coverage for audited integration boundaries."""
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import artifacts
from pco_api import WorkflowError
import setlist as sl
import test_setlist as fixtures
import supplement
from test_support import FAKE_MP3, catalog, visual_fixture


class RepairTests(unittest.TestCase):
    setUp = fixtures.SetlistTests.setUp
    ready = fixtures.SetlistTests.ready
    downloaded_material = fixtures.SetlistTests.downloaded_material
    def test_delivery_gate_checks_bytes_not_only_stage(self):
        payload = self.ready()
        audio = self.state["results"]["1"]["audio"]
        Path(audio["path"]).write_bytes(b"tampered")
        with self.assertRaises(WorkflowError):
            sl.propose(self.state, payload)

    def test_response_route_is_not_discarded(self):
        payload = self.ready()
        payload["response"].update(
            route=[{"label": "C", "cue": ""}, {"label": "B", "cue": ""}],
            structure_basis="Current musician arrangement")
        part = sl.propose(self.state, payload)
        self.assertEqual(part["selection"]["response"]["route"], payload["response"]["route"])
        result = sl.export(self.state, self.root / "response.docx", draft=True)
        from docx import Document
        self.assertNotIn("结构一致", "\n".join(p.text for p in Document(result["path"]).paragraphs))

    def test_response_unknown_fields_and_unheard_key_rejected(self):
        payload = self.ready()
        for extra in ({"bogus": 1}, {"key": "F#"}, {"bpm": 72}):
            p = copy.deepcopy(payload)
            p["response"].update(extra)
            with self.assertRaises(WorkflowError):
                sl.propose(self.state, p)

    def test_verified_score_import_and_tamper(self):
        path = self.root / "score.pdf"
        path.write_bytes(b"%PDF-isolated-test-source")
        self.state["results"]["2"] = {
            "status": "downloaded", "kind": "numbered_score",
            "path": str(path), "sha256": sl.rag.sha(path),
        }
        payload = {
            "song_id": "2", "version_basis": "fixture score identity",
            "structure_basis": "verified score page 1",
            "lyric_sections": [{"label": "V", "lines": ["测试原始歌词"]}],
            "route": [{"label": "IN", "cue": ""}, {"label": "V", "cue": ""}],
            "analysis": {"source_sha256": sl.rag.sha(path), "unknowns": [],
                         "confirmed": True, "user_message": "Isolated verified analysis fixture",
                         "evidence": [{"target": "lyrics:V", "page": 1},
                                      {"target": "route:0", "page": 1},
                                      {"target": "route:1", "page": 1}]},
        }
        for mutation in (
            {"confirmed": False}, {"source_sha256": "wrong"}, {"unknowns": ["unresolved bridge"]},
            {"evidence": [{"target": "lyrics:V", "page": 1}]},
        ):
            p = copy.deepcopy(payload)
            p["analysis"].update(mutation)
            with self.assertRaises(WorkflowError):
                sl.prepare(self.state, p, self.root, self.base)
        entry = sl.prepare(self.state, payload, self.root, self.base)
        self.assertEqual(entry["origin"], "verified_analysis")
        self.assertEqual(sl.material(self.state, "2")[0]["locator"]["analysis"], payload["analysis"])

    def test_palette_aliases(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "repair_renderer", Path(sl.rag.__file__).with_name("template_renderer.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for label in ("Pre-Chorus", "Pre Chorus", "PreChorus2", "PC", "PC2"):
            self.assertEqual(mod.TemplateRenderer.palette(label), mod.TemplateRenderer.palette("C"))

    def test_cleanup_protects_final_source_and_modified_temp(self):
        payload = self.ready()
        sl.propose(self.state, payload)
        result = sl.export(self.state, self.root / "final.docx", draft=True)
        final = Path(result["path"])
        original = final.read_bytes()
        temp = self.root / "render.png"
        temp.write_bytes(b"render")
        artifacts.register(self.state, temp, "temporary", final)
        with self.assertRaises(WorkflowError):
            artifacts.cleanup(self.state, {"apply": True})
        artifacts.review(self.state, {"path": str(final), "sha256": sl.rag.sha(final),
                                     "confirmed": True, "user_message": "test visual QA",
                                     "visual": visual_fixture(),
                                     "checks": ["layout", "colors", "content", "pagination"]})
        self.assertEqual(len(artifacts.cleanup(self.state, {})["candidates"]), 1)
        temp.write_bytes(b"user edit")
        self.assertFalse(artifacts.cleanup(self.state, {"apply": True})["deleted"])
        self.assertEqual(final.read_bytes(), original)
        self.assertTrue(self.source.exists())

    def response_audio(self, sid):
        song = copy.deepcopy(self.state["proposals"][0]["songs"][0])
        song.update(song_id=sid, arrangement_id=str(int(sid) + 10))
        if sid == "4":
            song["title"] = "新增回应"
        api = Mock()
        api.inspect.return_value = [song]
        api.file.return_value = FAKE_MP3
        rows = catalog(sid)
        rows[0]["label"] = "F"
        with patch("keys.inventory", return_value=rows), patch("keys.validate_mp3"):
            supplement.options(self.state, {"song_id": sid, "arrangement_id": song["arrangement_id"]}, api)
            sample = supplement.preview(self.state, {"key_id": rows[0]["key_id"],
                                        "attachment_id": rows[0]["audios"][0]["attachment_id"]},
                                        api, self.root / "response-preview")
            with self.assertRaises(WorkflowError):
                supplement.confirm(self.state, {"sha256": sample["sha256"], "confirmed": True,
                                               "listened": False, "user_message": "test"},
                                   api, self.root / "response-downloads")
            supplement.confirm(self.state, {"sha256": sample["sha256"], "confirmed": True,
                                           "listened": True, "user_message": "AUTOMATION ONLY"},
                               api, self.root / "response-downloads")
        return song, api

    def test_changed_response_key_uses_independent_audition_and_detects_tamper(self):
        payload = self.ready()
        self.response_audio("1")
        payload["response"].update(key="F", bpm=68, bpm_basis="current musician fixture")
        part = sl.propose(self.state, payload)
        self.assertEqual(part["selection"]["response"]["key"], "F")
        self.assertTrue(part["selection"]["response"]["full_structure"])
        Path(self.state["supplement"]["audio"]["path"]).write_bytes(b"tampered")
        with self.assertRaises(WorkflowError):
            sl.export(self.state, self.root / "bad.docx", draft=True)

    def test_new_response_material_and_full_export(self):
        payload = self.ready()
        song, _ = self.response_audio("4")
        dbpath = self.state["materials"]["1"]["database"]
        r = sl.record(dbpath, self.ids["1"])
        r.update(id="4"*24, title=song["title"], aliases=[])
        r.pop("citation", None)
        with sl.rag.connect(dbpath) as db:
            db.execute("INSERT INTO songs VALUES (?,?,?,?,?,?)",
                       (r["id"], r["source_id"], r["title"], "main",
                        sl.rag.dump(r), sl.rag.vector(r["title"])))
        sl.prepare(self.state, {"song_id": "4", "id": r["id"],
                               "version_basis": "isolated supplemental identity"}, self.root, self.base)
        payload["response"] = {"song_id": "4", "scripture": copy.deepcopy(fixtures.VERSE)}
        part = sl.propose(self.state, payload)
        self.assertEqual(part["selection"]["response"]["key"], "F")
        result = sl.export(self.state, self.root / "new-response.docx", draft=True)
        from docx import Document
        text = "\n".join(p.text for p in Document(result["path"]).paragraphs)
        self.assertIn("新增回应  调=F  BPM=", text)
        self.assertEqual(len(Document(result["path"]).tables), 5)

    def test_current_cues_and_event_metadata_do_not_copy_historical_personnel(self):
        payload = self.ready()
        payload["event"] = {"title": "Current fixture worship", "date": "2026-11-03",
                            "personnel": "Current musicians", "basis": "current user input"}
        payload["presentation"] = {"show_event_metadata": True}
        payload["main"][0].update(
            route=[{"label": "IN", "cue": "8 historical-name", "current_cue": "4 Current keys"},
                   {"label": "C", "cue": "", "current_cue": "Current group"}],
            structure_basis="current musician fixture")
        sl.propose(self.state, payload)
        result = sl.export(self.state, self.root / "metadata.docx", draft=True)
        from docx import Document
        doc = Document(result["path"])
        self.assertIn("Current fixture worship", "\n".join(p.text for p in doc.paragraphs))
        text = " ".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
        self.assertIn("4 Current keys", text)
        self.assertNotIn("historical-name", text)

    def test_cleanup_only_registered_unchanged_temps_and_idempotent(self):
        sl.propose(self.state, self.ready())
        result = sl.export(self.state, self.root / "owned.docx", draft=True)
        final = Path(result["path"])
        owned, other = self.root / "owned.png", self.root / "user.png"
        owned.write_bytes(b"generated")
        other.write_bytes(b"keep")
        artifacts.register(self.state, owned, "temporary", final)
        artifacts.review(self.state, {"path": str(final), "sha256": sl.rag.sha(final),
                                     "confirmed": True, "user_message": "test",
                                     "visual": visual_fixture(),
                                     "checks": ["layout", "colors", "content", "pagination"]})
        self.assertEqual(len(artifacts.cleanup(self.state, {"apply": True})["deleted"]), 1)
        self.assertFalse(artifacts.cleanup(self.state, {"apply": True})["deleted"])
        self.assertTrue(other.exists())

    def test_missing_delivery_and_bad_analysis_citations_rejected(self):
        payload = self.ready()
        for sid in ("1", "2", "3"):
            audio = self.state["results"][sid].pop("audio")
            with self.assertRaises(WorkflowError):
                sl.propose(self.state, payload)
            self.state["results"][sid]["audio"] = audio

    def test_obsolete_draft_pair_can_be_explicitly_retired_without_active_dangling_refs(self):
        sl.propose(self.state, self.ready())
        old = sl.export(self.state, self.root / "old-draft.docx", draft=True)
        final = sl.export(self.state, self.root / "replacement.docx", draft=True)
        artifacts.review(self.state, {"path": final["path"], "sha256": final["sha256"],
                                     "confirmed": True, "user_message": "test visual acceptance",
                                     "visual": visual_fixture(),
                                     "checks": ["layout", "colors", "content", "pagination"]})
        artifacts.retire(self.state, {"path": old["path"], "sha256": old["sha256"],
                                     "replacement": final["path"], "confirmed": True,
                                     "user_message": "test obsolete draft replacement"})
        result = artifacts.cleanup(self.state, {"apply": True})
        self.assertEqual(set(result["deleted"]), {old["path"], old["sources"]})
        self.assertTrue(Path(final["path"]).exists())
        self.assertTrue(Path(final["sources"]).exists())
        self.assertTrue(self.state["setlist"]["exports"][0]["retired_for_cleanup"])

    def test_retire_user_edited_draft_rejected(self):
        sl.propose(self.state, self.ready())
        old = sl.export(self.state, self.root / "edited.docx", draft=True)
        final = sl.export(self.state, self.root / "replacement.docx", draft=True)
        artifacts.review(self.state, {"path": final["path"], "sha256": final["sha256"],
                                     "confirmed": True, "user_message": "test",
                                     "visual": visual_fixture(),
                                     "checks": ["layout", "colors", "content", "pagination"]})
        Path(old["path"]).write_bytes(b"user changes")
        with self.assertRaises(WorkflowError):
            artifacts.retire(self.state, {"path": old["path"], "sha256": old["sha256"],
                                         "replacement": final["path"], "confirmed": True, "user_message": "test"})

    def test_supplemental_resources_preserve_chinese_priority_and_rag_skip(self):
        import downloads
        self.ready()
        _, api = self.response_audio("4")
        source = {"id": "lyrics:4", "kind": downloads.CHINESE_LYRICS, "scope": "selected",
                  "downloadable": True, "text": "这是测试中文歌词\n只用于隔离测试", "filename": "response.txt"}
        with patch("downloads.inventory", return_value=[source]):
            inspected = supplement.resources(self.state, {"inspect_only": True}, api, self.root)
            self.assertNotIn("text", inspected["sources"][0])
            with self.assertRaises(WorkflowError):
                supplement.resources(self.state, {"review": {
                    "classifications": [{"source_id": "lyrics:4", "kind": "other",
                                         "evidence": "attempted priority bypass"}]}}, api, self.root)
            result = supplement.resources(self.state, {}, api, self.root)
            self.assertEqual(Path(result["path"]).read_text(), source["text"])
        self.response_audio("1")
        with patch("downloads.inventory") as inventory:
            result = supplement.resources(self.state, {}, api, self.root)
            self.assertEqual(result["status"], "reused")
            inventory.assert_not_called()

    def test_missing_final_sources_blocks_review_and_cleanup(self):
        sl.propose(self.state, self.ready())
        result = sl.export(self.state, self.root / "sources-protected.docx", draft=True)
        final = Path(result["path"])
        payload = {"path": str(final), "sha256": result["sha256"], "confirmed": True,
                   "checks": ["layout", "colors", "content", "pagination"], "user_message": "test",
                   "visual": visual_fixture()}
        artifacts.review(self.state, payload)
        temp = self.root / "preview.png"
        temp.write_bytes(b"generated")
        artifacts.register(self.state, temp, "temporary", final)
        Path(result["sources"]).unlink()
        with self.assertRaises(WorkflowError):
            artifacts.review(self.state, payload)
        self.assertEqual(artifacts.cleanup(self.state, {"apply": True})["deleted"], [])
        self.assertTrue(temp.exists())

    def test_raw_rag_override_cannot_invent_LL(self):
        part = sl.propose(self.state, self.ready())
        selection = copy.deepcopy(part["selection"])
        selection["main"][0].update(route=[{"label": "LL"}], structure_basis="invalid fixture")
        with self.assertRaises(ValueError):
            sl.rag.assemble(part["database"], selection, self.root / "invented.docx")


if __name__ == "__main__":
    unittest.main()
