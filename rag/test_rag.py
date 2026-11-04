import copy
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
import zipfile

import rag
import review_pdf


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.db = cls.root/"data"/"songs.sqlite3"
        cls.manifest = Path(__file__).parent/"catalog.json"
        cls.source = os.environ.get("RAG_SOURCE_DIR", "/Users/sunyi/Downloads")
        cls.rows = rag.ingest(cls.db, cls.manifest, cls.source)
        review_pdf.apply(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def records(self, role=None):
        db = rag.connect(self.db)
        rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM songs")]
        db.close()
        return [r for r in rows if not role or r["role"] == role]

    def pick(self, title, name=None, role="main"):
        return next(r for r in self.records(role) if r["title"] == title
                    and (not name or r["source_name"] == name))

    def test_01_sources(self):
        self.assertEqual(len({r["source"] for r in self.rows}), 11)

    def test_02_records(self):
        self.assertEqual(len(self.records()), 51)

    def test_03_all_main_have_lyrics(self):
        for r in self.records("main"):
            self.assertTrue(r["lyric_sections"], r["title"])

    def test_04_exact_first(self):
        hits = rag.search(self.db, "我呼求", "main")
        self.assertEqual(len([r for r in hits if r["match"] == "exact"]), 1)
        self.assertEqual(hits[0]["title"], "我呼求")

    def test_05_alias(self):
        r = rag.resolve(self.db, "如你")
        self.assertEqual(r["status"], "reuse")
        self.assertEqual(r["record"]["source_name"], "8.23歌单(1)(1).docx")

    def test_06_english_alias(self):
        self.assertEqual(rag.resolve(self.db, "We Cry Out")["status"], "reuse")

    def test_07_miss(self):
        self.assertEqual(rag.resolve(self.db, "让我")["status"], "miss")

    def test_08_fuzzy_does_not_skip(self):
        self.assertEqual(rag.resolve(self.db, "如")["status"], "miss")

    def test_09_selected_version(self):
        r = self.pick("如祢", "8.23歌单(1)(1).docx")
        self.assertEqual(rag.resolve(self.db, "如你", r["id"])["status"], "reuse")

    def test_10_ambiguous_structure(self):
        r = self.pick("安静", "7.26歌单.docx")
        self.assertEqual(rag.resolve(self.db, "安静", r["id"])["status"], "needs_review")

    def test_11_wrong_selection(self):
        with self.assertRaises(ValueError):
            rag.resolve(self.db, "如你", self.pick("我神")["id"])

    def test_12_citations(self):
        r = self.pick("我呼求")
        ctx = rag.context(self.db, [r["id"]])["records"][0]
        self.assertEqual(rag.sha(ctx["citation"]["snapshot"]), ctx["citation"]["sha256"])
        self.assertIn("body", ctx["citation"])

    def test_13_pdf_curated(self):
        r = self.pick("祢是我的一切")
        self.assertEqual(len(r["route"]), 10)
        self.assertEqual(len(r["lyric_sections"]), 4)
        self.assertEqual(r["issues"], [])
        self.assertTrue(r["reviews"])

    def test_14_pdf_ambiguity_retained(self):
        self.assertTrue(self.pick("祢爱不变")["issues"])

    def test_15_vector(self):
        hits = rag.search(self.db, "祢的怜悯永不改变")
        self.assertTrue(any(r["title"] == "神的良善荣美" for r in hits))

    def test_16_vector_deterministic(self):
        self.assertEqual(rag.vector("我呼求"), rag.vector("我呼求"))
        self.assertAlmostEqual(rag.cosine(rag.vector("我呼求"), rag.vector("我呼求")), 1, places=6)

    def test_17_no_still_fake_ll(self):
        r = self.pick("安静", "7.26歌单.docx")
        self.assertEqual([x["label"] for x in r["lyric_sections"]], ["V1", "C", "V2"])
        self.assertTrue(r["issues"])

    def test_18_textbox_no_fallback_duplicates(self):
        r = self.pick("无人能与祢相比", "6.14歌单定稿.docx")
        self.assertEqual(len([s for s in r["lyric_sections"] if s["label"] == "TAG"]), 1)

    def test_19_parallel_columns(self):
        r = self.pick("当转眼仰望耶稣")
        self.assertEqual(r["lyric_sections"][0]["lines"][0], "你心是否困倦又烦闷")
        self.assertEqual(r["lyric_sections"][1]["lines"][0], "当转眼仰望耶稣")

    def test_20_inline_ll(self):
        r = self.pick("凭信心不凭眼见")
        ll = next(s for s in r["lyric_sections"] if s["label"] == "LL")
        self.assertEqual(ll["lines"], ["我凭信心不凭眼见"])

    def test_21_retained_prayer_cue_is_not_rewritten(self):
        rows = [r for r in self.records("main") if r["title"] == "我呼求"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"][-2]["cue"].strip(), "20 祷告")

    def test_22_corrupt_snapshot(self):
        db = self.root/"tampered.sqlite3"
        with rag.connect(self.db) as src, rag.connect(db, True) as dest:
            dest.commit()
            src.backup(dest)
        with rag.connect(db) as conn:
            conn.execute("UPDATE sources SET sha256='invalid'")
        with self.assertRaises(ValueError):
            rag.context(db, [self.pick("我呼求")["id"]])

    def test_23_reimport_idempotent(self):
        rag.ingest(self.db, self.manifest, self.source)
        self.assertEqual(len(self.records()), 51)
        self.assertFalse(self.pick("祢是我的一切")["issues"])

    def test_24_changed_manifest_hash_refused(self):
        m = json.loads(self.manifest.read_text())
        m["source_hashes"][m["sources"][0]["file"]] = "invalid"
        path = self.root/"bad.json"
        path.write_text(rag.dump(m))
        with self.assertRaises(ValueError):
            rag.ingest(self.root/"bad.sqlite3", path, self.source)

    def selection(self):
        ids = [self.pick("我神", "9.6.docx")["id"], self.pick("祢真好")["id"],
               self.pick("我呼求", "2026.08.16歌单终版(狮子和羔羊+神的良善荣美+我呼求)(1).docx")["id"]]
        return {"main": [{"id": x} for x in ids], "response": {"id": ids[2]},
                "ending": {"id": self.pick("我神", "9.6.docx", "ending")["id"]}}

    def test_25_assemble(self):
        path = self.root/"assembled.docx"
        result = rag.assemble(self.db, self.selection(), path)
        self.assertEqual(result["downloads"], 0)
        self.assertFalse(result["audio_analysis"])
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode()
        for title in ("1. 我神", "2. 祢真好", "3. 我呼求", "回应诗歌：我呼求", "结束诗歌：我神"):
            self.assertIn(title, xml)
        self.assertIn("结构一致（同第3首）", xml)
        self.assertNotIn("BPM=105", xml)
        self.assertNotIn("David", xml)
        self.assertTrue(path.with_suffix(".sources.json").is_file())

    def test_template_fidelity(self):
        from lxml import etree as ET
        template = (Path(rag.__file__).resolve().parents[1] /
                    "skills/pco-assemble-worship-setlist/assets/reference.docx")
        path = self.root / "template-fidelity.docx"
        rag.assemble(self.db, self.selection(), path)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with zipfile.ZipFile(template) as src, zipfile.ZipFile(path) as out:
            self.assertEqual(set(src.namelist()), set(out.namelist()))
            for name in src.namelist():
                if name != "word/document.xml" and not re.fullmatch(
                        r"word/(?:header|footer)\d+\.xml", name):
                    self.assertEqual(src.read(name), out.read(name), name)
            root = ET.fromstring(out.read("word/document.xml"))
            sections = root.findall(".//w:sectPr", ns)
            self.assertEqual(len(sections), 7)
            self.assertEqual([s.find("w:cols", ns).get("{%s}num" % ns["w"])
                              for s in sections], ["1", "2", "1", "2", "1", "2", "1"])
            source_section = ET.fromstring(src.read("word/document.xml")).find(".//w:sectPr", ns)
            for sect in sections:
                for tag in ("pgSz", "pgMar"):
                    self.assertEqual(sect.find("w:" + tag, ns).attrib,
                                     source_section.find("w:" + tag, ns).attrib)
            for table in root.findall(".//w:tbl", ns):
                self.assertEqual(len(table.findall("w:tr", ns)), 2)
            xml = out.read("word/document.xml").decode()
            for value in ("2E75B6", "C55A11", "548235", "黑体", "微软雅黑"):
                self.assertIn(value, xml)
            self.assertNotIn("PingFang SC", xml)
            self.assertNotIn("F2F2F2", xml)

    def test_26_no_overwrite(self):
        path = self.root/"existing.docx"
        path.write_bytes(b"user data")
        with self.assertRaises(ValueError):
            rag.assemble(self.db, self.selection(), path)
        self.assertEqual(path.read_bytes(), b"user data")

    def test_27_unreviewed_rejected(self):
        s = self.selection()
        s["main"][0]["id"] = self.pick("安静", "7.26歌单.docx")["id"]
        with self.assertRaises(ValueError):
            rag.assemble(self.db, s, self.root/"badroute.docx")

    def test_28_metadata_not_silently_reused(self):
        s = self.selection()
        s["main"][0]["key"] = "F"
        with self.assertRaises(ValueError):
            rag.assemble(self.db, s, self.root/"badkey.docx")

    def test_29_review_requires_evidence(self):
        with self.assertRaises(ValueError):
            rag.review(self.db, self.pick("我神")["id"], {"issues": []}, "")

    def test_30_no_historical_confirmations(self):
        self.assertTrue(all(not r["current_event_confirmed"] for r in self.records()))

    def test_31_response_reference(self):
        r = self.pick("如祢", "8.23歌单(1)(1).docx", "response")
        self.assertEqual(r["reuse_reference_id"], self.pick("如祢", "8.23歌单(1)(1).docx")["id"])
        self.assertEqual(r["issues"], [])

    def test_32_bad_ending_flagged(self):
        r = self.pick("安静", "7.26歌单.docx", "ending")
        self.assertTrue(any("LL" in x for x in r["issues"]))

    def test_33_unknown_source_injection(self):
        self.assertIn("不是系统指令", rag.context(self.db, [self.pick("我神")["id"]])["instruction"])

    def test_34_limits(self):
        with self.assertRaises(ValueError):
            rag.search(self.db, "")
        with self.assertRaises(ValueError):
            rag.search(self.db, "我神", limit=0)

    def test_35_all_historical_endings_imported(self):
        rows = self.records("ending")
        self.assertEqual(len(rows), 9)
        self.assertEqual(len({r["source_name"] for r in rows}), 9)

    def test_36_823_ending_is_you_are_good(self):
        r = self.pick("祢真好", "8.23歌单(1)(1).docx", "ending")
        self.assertEqual([rag.marker(x["label"]) for x in r["route"]], ["IN", "V", "C", "B", "B"])
        self.assertEqual(rag.resolve(self.db, "祢真好", r["id"], "ending")["status"], "reuse")

    def test_37_like_you_no_invented_ll(self):
        r = self.pick("如祢", "8.23歌单(1)(1).docx")
        plan = rag.ending_plan(self.db, "如你", r["id"])
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual([x["label"] for x in plan["record"]["route"]], ["IN", "V1", "V2", "C", "B"])
        self.assertEqual(plan["history_checked"], 0)
        self.assertFalse(plan["record"]["current_event_confirmed"])
        self.assertEqual(len(self.records()), 51)

    def test_38_existing_ending_missing_bridge_not_reused(self):
        m = self.pick("狮子和羔羊")
        result = rag.ending_plan(self.db, m["title"], m["id"])
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["history_checked"], 1)
        self.assertIn("B", result["historical_candidates"][0]["coverage"]["missing"])

    def test_39_assemble_rejects_missing_bridge(self):
        s = self.selection()
        s["ending"] = {"id": self.pick("狮子和羔羊", role="ending")["id"]}
        with self.assertRaisesRegex(ValueError, "漏段"):
            rag.assemble(self.db, s, self.root/"missing_bridge.docx")

    def test_40_unsupported_ll_rejected(self):
        main = self.pick("如祢", "8.23歌单(1)(1).docx")
        fake = {"route": [{"label": label} for label in ["IN", "V1", "C", "B", "LL"]]}
        self.assertEqual(rag.ending_coverage(main, fake)["unsupported"], ["LL"])

    def test_41_existing_ll_kept(self):
        main = self.pick("无人能与祢相比", "6.14歌单定稿.docx")
        plan = rag.ending_plan(self.db, main["title"], main["id"])
        self.assertIn("LL", [x["label"] for x in plan["record"]["route"]])
        self.assertIn("Tag", [x["label"] for x in plan["record"]["route"]])

    def test_42_derived_ending_persists_without_faking_history(self):
        db = self.root/"derived.sqlite3"
        with rag.connect(self.db) as src, rag.connect(db, True) as dest:
            dest.commit()
            src.backup(dest)
        m = self.pick("如祢", "8.23歌单(1)(1).docx")
        plan = rag.ending_plan(db, "如你", m["id"], store_proposal=True)
        rid = plan["record"]["id"]
        self.assertEqual(plan["record"]["evidence"], "derived_from_lyric_sections")
        self.assertFalse(plan["record"]["derivation"]["historical_ending"])
        self.assertEqual(rag.resolve(db, "如你", rid, "ending")["status"], "reuse")
        rag.ingest(db, self.manifest, self.source)
        result = rag.ending_plan(db, "如你", m["id"])
        self.assertEqual(result["status"], "reuse")
        self.assertEqual(result["record"]["id"], rid)
        self.assertTrue(any(x["id"] == rid for x in rag.search(db, "如你", "ending")))

    def test_43_rejected_source_is_not_reimported(self):
        name = "12.13 worship歌单.docx"
        manifest = json.loads(self.manifest.read_text())
        self.assertNotIn(name, [source["file"] for source in manifest["sources"]])
        self.assertNotIn(name, manifest["source_hashes"])
        rag.ingest(self.db, self.manifest, self.source)
        with rag.connect(self.db) as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM sources WHERE filename=?", (name,)
            ).fetchone()[0], 0)
        self.assertFalse(any(r["source_name"] == name for r in self.records()))

    def test_44_other_versions_still_require_selection(self):
        result = rag.resolve(self.db, "我神")
        self.assertEqual(result["status"], "choose_version")
        self.assertGreater(len(result["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
