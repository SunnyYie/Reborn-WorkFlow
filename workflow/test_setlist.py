"""Isolated mixed RAG/PCO scenario; no claims of real human audition."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from docx import Document

import downloads
import engine as e
import keys
from pco_api import WorkflowError
import setlist as sl
import spoken
from test_spoken import draft
from test_support import VOCALS, approve
from test_workflow import LYRICS, prepared, proposal, resource

VERSE = {
    "reference": "诗篇51:10", "translation": "和合本",
    "quote": "神啊，求你为我造清洁的心，使我里面重新有正直的灵。",
    "source": "isolated scripture fixture; not live verification", "verification": "user_provided",
}


class SetlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "base.sqlite3"
        self.source = self.root / "historical.docx"
        doc = Document()
        doc.add_paragraph("isolated source")
        doc.save(self.source)
        self.ids = {}
        with sl.rag.connect(self.base, create=True) as db:
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?)",
                       ("source", str(self.source), str(self.source), self.source.name, sl.rag.sha(self.source)))
            for i, title in ((1, "我呼求"), (3, "如祢")):
                rid = str(i) * 24
                r = {
                    "id": rid, "source_id": "source", "source_name": self.source.name,
                    "title": title, "aliases": ["如你"] if i == 3 else [], "role": "main",
                    "locator": {"body": [0, 1]}, "blocks": [], "issues": [],
                    "lyric_sections": [{"label": label, "lines": ["隔离测试歌词" + label]}
                                       for label in ("V1", "C", "B")],
                    "route": [{"label": label, "cue": ""} for label in ("IN", "V1", "C", "B")],
                    "evidence": "test_fixture", "current_event_confirmed": False, "meter": "4/4",
                }
                db.execute("INSERT INTO songs VALUES (?,?,?,?,?,?)",
                           (rid, "source", title, "main", sl.rag.dump(r), sl.rag.vector(title)))
                self.ids[str(i)] = rid
        self.state = prepared()
        self.state["test_run"] = True
        for i, name in enumerate(("我呼求", "让我", "如你"), 1):
            self.state["candidates"][f"{i}:{i+10}"]["title"] = name
        for name in ("我呼求", "让我", "如你"):
            sl.search(self.state, name, self.base)
        e.propose(self.state, proposal())
        e.confirm(self.state, {"proposal_id": "A", "revision": self.state["revision"],
                              "confirmed": True, "user_message": "AUTOMATION ONLY simulated plan approval"})
        for sid, rid in self.ids.items():
            sl.prepare(self.state, {"song_id": sid, "id": rid, "version_basis": "isolated identity"},
                       self.root, self.base)

    def downloaded_material(self):
        path = self.root / "lyrics.txt"
        path.write_text(LYRICS, encoding="utf-8")
        self.state["results"]["2"] = {
            "status": "downloaded", "kind": "chinese_lyrics",
            "path": str(path), "sha256": sl.rag.sha(path),
        }
        return {"song_id": "2", "version_basis": "original downloaded source",
                "structure_basis": "test proposal, not audio reconstruction",
                "lyric_sections": [{"label": "V", "lines": LYRICS.splitlines()[:2]},
                                   {"label": "C", "lines": LYRICS.splitlines()[2:]}],
                "route": [{"label": x, "cue": ""} for x in ("IN", "V", "C")]}

    def ready(self):
        approve(self.state, self.root / "previews")
        sl.prepare(self.state, self.downloaded_material(), self.root, self.base)
        for song in keys.confirmed(self.state)["songs"]:
            preview = self.state["keys"]["previews"][keys.confirmed(self.state)["id"]][song["song_id"]]
            self.state["results"].setdefault(song["song_id"], {})["audio"] = {
                **copy.deepcopy(preview), "status": "downloaded"}
        spoken.requirements(self.state, {})
        spoken.propose(self.state, draft())
        spoken.confirm(self.state, {"revision": self.state["spoken"]["revision"], "confirmed": True,
                                   "user_message": "AUTOMATION ONLY simulated spoken approval"})
        return {
            "main": [{"song_id": str(i), "scripture": copy.deepcopy(VERSE)} for i in (1, 2, 3)],
            "response": {"song_id": "1"}, "ending": {"song_id": "3"},
        }

    def test_rag_hit_alias_and_miss(self):
        self.assertEqual(self.state["retrieval"]["如你"]["result"]["status"], "reuse")
        self.assertEqual(self.state["retrieval"]["让我"]["result"]["status"], "miss")

    def test_full_theme_to_final_docx_mixed_sources(self):
        payload = self.ready()
        part = sl.propose(self.state, payload)
        before = self.root / "unapproved.docx"
        with self.assertRaises(WorkflowError):
            sl.export(self.state, before)
        self.assertFalse(before.exists())
        sl.confirm(self.state, {"revision": part["revision"], "confirmed": True,
                                "user_message": "AUTOMATION ONLY simulated final approval"})
        result = sl.export(self.state, self.root / "complete.docx")
        self.assertEqual(result["status"], "test_artifact")
        doc = Document(result["path"])
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("非现场定稿", text)
        self.assertIn("衔接话语", text)
        self.assertIn(VERSE["quote"], text)
        self.assertEqual(text.count("【祷告】"), 1)
        self.assertEqual(text.count("【衔接话语】"), 3)
        ending = sl.record(part["database"], part["selection"]["ending"]["id"])
        self.assertEqual([r["label"] for r in ending["route"]], ["IN", "V1", "C", "B"])
        self.assertNotIn("LL", [r["label"] for r in ending["route"]])
        sidecar = json.loads(Path(result["sources"]).read_text())
        self.assertTrue(sidecar["selection"]["test_run"])

    def test_rag_reuse_does_not_inventory_or_download_documents(self):
        approve(self.state, self.root / "previews")
        def inventory(api, song):
            self.assertEqual(song["song_id"], "2")
            return []
        with patch("downloads.inventory", side_effect=inventory) as query, patch("keys.deliver"):
            downloads.download(self.state, Mock(), self.root / "out")
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.state["results"]["1"]["status"], "reused")
        self.assertEqual(self.state["results"]["3"]["status"], "reused")
        self.assertEqual(self.state["results"]["2"]["status"], "missing")

    def test_unmatched_title_rejected(self):
        with self.assertRaises(WorkflowError):
            sl.prepare(self.state, {"song_id": "2", "id": self.ids["1"],
                                    "version_basis": "wrong"}, self.root, self.base)

    def test_cannot_invent_lyrics_or_LL(self):
        payload = self.downloaded_material()
        for change in (
            {"lyric_sections": [{"label": "V", "lines": ["not in original"]}]},
            {"route": [{"label": "LL"}]},
            {"lyric_sections": [{"label": "LL", "lines": LYRICS.splitlines()}]},
        ):
            with self.assertRaises(WorkflowError):
                sl.prepare(self.state, payload | change, self.root, self.base)

    def test_material_source_tamper_blocked(self):
        payload = self.ready()
        self.source.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            sl.propose(self.state, payload)

    def test_scripture_verification_and_bpm_basis(self):
        payload = self.ready()
        for bad in ("verse", "bpm"):
            p = copy.deepcopy(payload)
            if bad == "verse":
                p["main"][0]["scripture"]["verification"] = "unverified"
            else:
                p["main"][0]["bpm"] = 70
            with self.assertRaises(WorkflowError):
                sl.propose(self.state, p)

    def test_material_and_song_order_change_invalidates_setlist(self):
        payload = self.ready()
        sl.propose(self.state, payload)
        spoken.feedback(self.state, {"message": "test revise speech"})
        with self.assertRaises(WorkflowError):
            sl.export(self.state, self.root / "stale.docx", draft=True)

    def test_key_change_invalidates_setlist(self):
        sl.propose(self.state, self.ready())
        keys.feedback(self.state, {"message": "test lower key"})
        with self.assertRaises(WorkflowError):
            sl.export(self.state, self.root / "stale.docx", draft=True)

    def test_wrong_order_and_stale_revision_rejected(self):
        payload = self.ready()
        payload["main"].reverse()
        with self.assertRaises(WorkflowError):
            sl.propose(self.state, payload)
        payload["main"].reverse()
        part = sl.propose(self.state, payload)
        with self.assertRaises(WorkflowError):
            sl.confirm(self.state, {"revision": part["revision"] - 1, "confirmed": True,
                                    "user_message": "test"})

    def test_missing_material_blocks_assembly(self):
        approve(self.state, self.root / "previews")
        with self.assertRaises(WorkflowError):
            sl.propose(self.state, {})

    def test_base_database_not_mutated(self):
        original = sl.rag.sha(self.base)
        sl.propose(self.state, self.ready())
        self.assertEqual(sl.rag.sha(self.base), original)

    def test_actual_cli_persists_and_exports_same_session(self):
        payload = self.ready()
        path = self.root / "state.json"
        path.write_text(json.dumps(self.state, ensure_ascii=False))
        cli = Path(__file__).with_name("workflow.py")
        def call(command, data=None, extra=()):
            completed = subprocess.run(
                [sys.executable, "-B", str(cli), "--state", str(path), command, *extra],
                input=json.dumps(data, ensure_ascii=False) if data else None,
                capture_output=True, text=True, check=True,
            )
            return json.loads(completed.stdout)
        part = call("setlist-propose", payload)
        call("setlist-confirm", {"revision": part["revision"], "confirmed": True,
                                "user_message": "AUTOMATION ONLY simulated final approval"})
        result = call("setlist-export", extra=("--output", str(self.root / "cli.docx")))
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(json.loads(path.read_text())["setlist"]["stage"], "confirmed")

    def test_no_overwrite_and_changed_source_after_confirmation_rejected(self):
        part = sl.propose(self.state, self.ready())
        sl.confirm(self.state, {"revision": part["revision"], "confirmed": True,
                                "user_message": "AUTOMATION ONLY approval"})
        output = self.root / "existing.docx"
        output.write_bytes(b"preserve")
        with self.assertRaises(ValueError):
            sl.export(self.state, output)
        self.assertEqual(output.read_bytes(), b"preserve")
        # Updating an ending payload must invalidate even with an unchanged source file.
        with sl.rag.connect(part["database"]) as db:
            rid = part["selection"]["ending"]["id"]
            r = json.loads(db.execute("SELECT payload FROM songs WHERE id=?", (rid,)).fetchone()[0])
            r["route"].append({"label": "LL", "cue": ""})
            db.execute("UPDATE songs SET payload=? WHERE id=?", (sl.rag.dump(r), rid))
        with self.assertRaises(WorkflowError):
            sl.export(self.state, self.root / "changed.docx")


if __name__ == "__main__":
    unittest.main()
