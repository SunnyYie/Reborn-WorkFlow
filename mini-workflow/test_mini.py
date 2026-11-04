"""Isolated fixtures only: no live PCO or native application observations."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import mini as m
import delivery as d
import artifacts
from pco_api import WorkflowError

LINES = ["这是一行用于测试的中文歌词并不是实际歌曲",
         "这是另一行用于核验的中文文字并不是实际歌曲"]


class FakePCO:
    def __init__(self):
        self.rows = {str(i): {"song_id": str(i), "arrangement_id": str(i + 10),
                             "title": f"测试歌曲{i}", "arrangement": "Fixture",
                             "bpm": 99, "meter": "4/4", "updated_at": "fixture",
                             "lyrics": "Verse\n" + LINES[0] + "\nChorus\n" + LINES[1]}
                     for i in range(1, 5)}
        self.attachments = {}
        self.files = {}
        self.calls = []
        self.error = None

    def catalog(self, query=None):
        self.calls.append(("catalog", query))
        if self.error:
            raise WorkflowError(self.error, "Simulated PCO failure")
        return [{"song_id": sid, "title": r["title"]} for sid, r in self.rows.items()
                if query is None or query in r["title"]]

    def inspect(self, sid):
        self.calls.append(("inspect", sid))
        return [copy.deepcopy(self.rows[sid])]

    def request(self, path):
        self.calls.append(("request", path))
        if self.error:
            raise WorkflowError(self.error, "Simulated PCO failure")
        sid = path.split("/")[4]
        return {"data": {"id": sid, "attributes": {"title": self.rows[sid]["title"]}}}

    def pages(self, path):
        self.calls.append(("pages", path))
        sid = path.split("/")[4]
        if path.endswith("/arrangements?per_page=100"):
            r = self.rows[sid]
            return [{"id": r["arrangement_id"], "attributes": {
                "name": r["arrangement"], "bpm": r["bpm"], "meter": r["meter"],
                "updated_at": r["updated_at"], "lyrics": r["lyrics"]}}]
        return copy.deepcopy(self.attachments.get(path, []))

    def file(self, aid):
        self.calls.append(("file", aid))
        return self.files[aid]

    def attachment(self, sid, aid, filename, content, downloadable=True):
        path = f"/services/v2/songs/{sid}/arrangements/{int(sid) + 10}/attachments"
        self.attachments.setdefault(path, []).append({
            "id": aid, "attributes": {"filename": filename, "downloadable": downloadable}})
        self.files[aid] = content


def fixture():
    api, state = FakePCO(), m.new_state()
    m.select(state, {"main": [{"title": api.rows[str(i)]["title"]} for i in (1, 2, 3)]}, api)
    return api, state


def prepare(state, sid="1", **changes):
    result = state["results"][sid + ":" + str(int(sid) + 10)]
    payload = {"song_id": sid, "arrangement_id": str(int(sid) + 10),
               "source_sha256": result["sha256"], "version_basis": "Simulated original arrangement",
               "lyric_sections": [{"label": "V", "lines": [LINES[0]]},
                                  {"label": "C", "lines": [LINES[1]]}],
               "route": [{"label": "V"}, {"label": "C"}],
               "structure_kind": "proposal", "structure_basis": "Simulated proposed route"}
    payload.update(changes)
    return m.prepare(state, payload)


def visual(exported):
    # Simulated only. These values never establish real WPS acceptance.
    result = {
        "app": "WPS", "page_count": 4,
        "pages": [{"page": n, "checks": sorted(artifacts.PAGE_CHECKS),
                   "blocks": [f"main:{n}"] if n < 4 else ["response", "ending"],
                   "note": "AUTOMATION ONLY simulated page"} for n in range(1, 5)],
        "songs": [], "response_ending": {
            "page": 4, "checks": sorted(artifacts.RESPONSE_CHECKS),
            "note": "AUTOMATION ONLY simulated supplementary page"}}
    for n in range(1, 4):
        row = {"position": n, "pages": [n], "lyrics_pages": [n],
               "checks": sorted(artifacts.SONG_CHECKS), "note": "AUTOMATION ONLY simulated song"}
        if not exported["features"][f"main:{n}"]["lyrics"]:
            omitted = {"heading_baseline", "first_lyric_baseline", "lyric_spacing",
                       "lyrics_pagination", "table_lyric_gap", "section_spacing"}
            row.update(lyrics_pages=[], checks=sorted(artifacts.SONG_CHECKS - omitted),
                       not_applicable={k: "AUTOMATION ONLY blank lyric fixture" for k in omitted})
        result["songs"].append(row)
    if not any(exported["features"][r]["table"] for r in ("response", "ending")):
        omitted = {"table_style", "table_dimensions", "instrument_notation"}
        result["response_ending"].update(
            checks=sorted(artifacts.RESPONSE_CHECKS - omitted),
            not_applicable={k: "AUTOMATION ONLY blank fixture" for k in omitted})
    return result


def acceptance(state, exported):
    candidates = [r for r in state["artifacts"] if r["path"] not in {
        exported["path"], exported["sources"]} and not r.get("deleted_at")]
    return {"path": exported["path"], "sha256": exported["sha256"],
            "confirmed": True, "user_message": "AUTOMATION ONLY simulated native inspector",
            "checks": ["layout", "colors", "content", "pagination"], "visual": visual(exported),
            "cleanup": {"user_message": "AUTOMATION ONLY simulated closed files",
                        "checked_files": [{"path": r["path"], "sha256": r["sha256"], "closed": True}
                                          for r in candidates]}}


class SelectionTests(unittest.TestCase):
    def test_order_three_and_optional_blanks(self):
        api, state = fixture()
        self.assertEqual([r["song_id"] for r in state["selection"]["main"]], ["1", "2", "3"])
        self.assertIsNone(state["selection"]["response"])
        self.assertIsNone(state["selection"]["ending"])
        for count in (0, 2, 4):
            with self.subTest(count=count), self.assertRaises(WorkflowError):
                m.select(state, {"main": [{"title": "测试歌曲1"}] * count}, api)
        with self.assertRaises(WorkflowError):
            m.select(state, {"main": [{"title": "测试歌曲1"}] * 3}, api)

    def test_response_reuses_main_and_new_ending(self):
        api, state = fixture()
        m.select(state, {"main": [{"title": f"测试歌曲{i}"} for i in (3, 2, 1)],
                         "response": {"main_position": 2}, "ending": {"title": "测试歌曲4"}}, api)
        self.assertEqual(state["selection"]["response"]["song_id"], "2")
        self.assertEqual(state["selection"]["ending"]["song_id"], "4")
        self.assertEqual(len(m.songs(state)), 4)

    def test_ambiguous_title_not_guessed(self):
        api, state = fixture()
        api.rows["4"]["title"] = api.rows["1"]["title"]
        with self.assertRaisesRegex(WorkflowError, "ambiguous"):
            m.select(state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, api)

    def test_multiple_arrangements_require_explicit_ids(self):
        api, state = fixture()
        first = api.inspect("1")[0]
        other = dict(first, arrangement_id="99")
        with patch.object(api, "inspect", return_value=[first, other]):
            with self.assertRaisesRegex(WorkflowError, "Multiple"):
                m.select(state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, api)
        m.inspect(state, api, ["1"])
        m.select(state, {"main": [{"song_id": "1", "arrangement_id": "11"},
                                  {"title": "测试歌曲2"}, {"title": "测试歌曲3"}]}, api)

    def test_not_found_differs_from_network_failure(self):
        api, state = fixture()
        data = {"main": [{"title": "不存在"}, {"title": "测试歌曲2"}, {"title": "测试歌曲3"}]}
        m.select(state, data, api)
        self.assertEqual(state["selection"]["main"][0]["status"], "not_found")
        api.error = "network_error"
        with self.assertRaises(WorkflowError):
            m.select(state, data, api)

    def test_reject_key_bpm_generated_spoken_and_unattributed_scripture(self):
        api, state = fixture()
        base = {"main": [{"song_id": str(i), "arrangement_id": str(i + 10)} for i in (1, 2, 3)]}
        for field in ("key", "bpm"):
            data = copy.deepcopy(base)
            data["main"][0][field] = "G"
            with self.assertRaises(WorkflowError):
                m.select(state, data, api)
        with self.assertRaises(WorkflowError):
            m.select(state, {**base, "spoken": [{"title": "Generated", "text": "Not user supplied"}]}, api)
        data = copy.deepcopy(base)
        data["main"][0]["scripture"] = {"quote": "Unverified"}
        with self.assertRaises(WorkflowError):
            m.select(state, data, api)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.api, self.state = fixture()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()

    def test_lyrics_win_and_no_audio_or_rag_calls(self):
        self.api.attachment("1", "101", "中文简谱.pdf", b"%PDF-fixture\n%%EOF")
        with patch.dict(sys.modules, {"rag": None, "keys": None, "setlist": None}):
            m.inventory(self.state, self.api)
            result = m.download(self.state, self.api, self.directory)
        self.assertTrue(all(r["kind"] == "chinese_lyrics" for r in result.values()))
        self.assertFalse(any(c[0] == "file" for c in self.api.calls))
        self.assertFalse(any("mp3" in str(c).lower() for c in self.api.calls))

    def test_docx_lyrics_before_score_when_field_empty(self):
        from io import BytesIO
        content = BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml",
                            '<w:document xmlns:w="urn:test"><w:p><w:r><w:t>中文测试歌词</w:t></w:r></w:p></w:document>')
        self.api.rows["1"]["lyrics"] = ""
        m.inspect(self.state, self.api, ["1"])
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, self.api)
        self.api.attachment("1", "101", "中文歌词.docx", content.getvalue())
        self.api.attachment("1", "102", "中文简谱.pdf", b"%PDF-fixture\n%%EOF")
        result = m.download(self.state, self.api, self.directory)
        self.assertEqual(result["1:11"]["source"]["attachment_id"], "101")
        self.assertNotIn(("file", "102"), self.api.calls)

    def test_score_fallback_missing_and_restricted_are_distinct(self):
        for sid in ("1", "2", "3"):
            self.api.rows[sid]["lyrics"] = ""
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, self.api)
        self.api.attachment("1", "101", "中文简谱.pdf", b"%PDF-fixture\n%%EOF")
        self.api.attachment("3", "103", "中文歌词.pdf", b"%PDF-fixture\n%%EOF", downloadable=False)
        result = m.download(self.state, self.api, self.directory)
        self.assertEqual(result["1:11"]["kind"], "numbered_score")
        self.assertEqual(result["2:12"]["status"], "missing")
        self.assertEqual(result["3:13"]["status"], "restricted")

    def test_restricted_lyrics_do_not_fallback_to_score(self):
        self.api.rows["1"]["lyrics"] = ""
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, self.api)
        self.api.attachment("1", "101", "中文歌词.pdf", b"%PDF-fixture\n%%EOF", False)
        self.api.attachment("1", "102", "中文简谱.pdf", b"%PDF-fixture\n%%EOF")
        self.assertEqual(m.download(self.state, self.api, self.directory)["1:11"]["status"], "restricted")
        self.assertNotIn(("file", "102"), self.api.calls)

    def test_errors_never_report_missing(self):
        for code in ("auth_error", "network_error"):
            with self.subTest(code=code):
                self.api.error = code
                result = m.download(self.state, self.api, self.directory)
                self.assertEqual(result["1:11"]["status"], code)
                self.assertNotEqual(result["1:11"]["status"], "missing")

    def test_auth_failure_records_unchecked_songs_without_inventing_missing(self):
        self.api.error = "auth_error"
        result = m.download(self.state, self.api, self.directory)
        self.assertEqual(result["1:11"]["status"], "auth_error")
        self.assertEqual(result["2:12"]["status"], "not_checked")
        self.assertEqual(result["3:13"]["status"], "not_checked")
        exported = d.export(self.state, self.directory / "auth-blocked.docx")
        self.assertTrue(any(w["status"] == "not_checked" for w in exported["warnings"]))

    def test_duplicate_optional_song_and_repeat_reuse_original(self):
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)],
                             "response": {"main_position": 1}, "ending": {"main_position": 1}}, self.api)
        result = m.download(self.state, self.api, self.directory)
        original = copy.deepcopy(result)
        m.download(self.state, self.api, self.directory)
        self.assertEqual(result, original)
        self.assertEqual(len(list(self.directory.rglob("*.txt"))), 3)

    def test_ambiguous_resource_needs_user_choice_and_recheck(self):
        self.api.rows["1"]["lyrics"] = ""
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, self.api)
        self.api.attachment("1", "101", "中文歌词1.pdf", b"%PDF-a\n%%EOF")
        self.api.attachment("1", "102", "中文歌词2.pdf", b"%PDF-b\n%%EOF")
        self.assertEqual(m.download(self.state, self.api, self.directory)["1:11"]["status"], "needs_review")
        review = {"song_id": "1", "arrangement_id": "11", "selected_source_id": "attachment:101",
                  "user_message": "Simulated user choice"}
        m.review_resources(self.state, review, self.api)
        self.assertEqual(m.download(self.state, self.api, self.directory)["1:11"]["source"]["attachment_id"], "101")
        self.api.attachments["/services/v2/songs/1/arrangements/11/attachments"][0]["attributes"]["updated_at"] = "new"
        self.assertEqual(m.download(self.state, self.api, self.directory)["1:11"]["status"], "needs_review")

    def test_changed_arrangement_requires_inspection_not_retry(self):
        self.api.rows["1"]["updated_at"] = "changed"
        self.assertEqual(m.download(self.state, self.api, self.directory)["1:11"]["status"], "needs_review")

    def test_bad_file_and_unknown_resource_not_silently_accepted(self):
        self.api.rows["1"]["lyrics"] = ""
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, self.api)
        self.api.attachment("1", "101", "中文歌词.pdf", b"<html>login</html>")
        self.assertEqual(m.download(self.state, self.api, self.directory)["1:11"]["status"], "invalid_file")


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.api, self.state = fixture()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        m.download(self.state, self.api, self.directory / "downloads")

    def output(self, name="setlist.docx"):
        return d.export(self.state, self.directory / name)

    def xml(self, exported):
        with zipfile.ZipFile(exported["path"]) as archive:
            return archive.read("word/document.xml").decode()

    def test_lyrics_source_hash_no_rewriting_ll_or_fake_route(self):
        for changes in (
            {"source_sha256": "wrong"},
            {"lyric_sections": [{"label": "V", "lines": ["编写的新词"]}]},
            {"lyric_sections": [{"label": "LL", "lines": [LINES[0]]}]},
            {"route": [{"label": "B"}]},
            {"route": [{"label": "V", "current_cue": "16"}]},
            {"key": "G"}, {"bpm": 99},
        ):
            with self.subTest(changes=changes), self.assertRaises(WorkflowError):
                prepare(self.state, **changes)
        prepare(self.state)

    def test_score_reading_requires_per_section_and_route_evidence(self):
        self.api.rows["1"]["lyrics"] = ""
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, self.api)
        self.api.attachment("1", "101", "中文简谱.pdf", b"%PDF-fixture\n%%EOF")
        m.download(self.state, self.api, self.directory / "downloads")
        with self.assertRaises(WorkflowError):
            prepare(self.state)
        analysis = {"verified": True, "method": "Simulated score reading", "note": "Fixture only",
                    "sections": {"V": "page 1 left", "C": "page 1 right"}}
        prepare(self.state, analysis=analysis)
        with self.assertRaises(WorkflowError):
            prepare(self.state, analysis=analysis, structure_kind="source_explicit")
        analysis["route"] = {"0": "page 1 top", "1": "page 1 bottom"}
        prepare(self.state, analysis=analysis, structure_kind="source_explicit")

    def test_template_blank_key_bpm_scripture_response_and_ending(self):
        for sid in ("1", "2", "3"):
            prepare(self.state, sid)
        exported = self.output()
        xml = self.xml(exported)
        self.assertEqual(xml.count("调=  BPM="), 3)
        self.assertNotIn("BPM=99", xml)
        self.assertNotIn("待本次确认", xml)
        self.assertNotIn("待确认", xml)
        self.assertNotIn("祷告与衔接话语", xml)
        self.assertIn("回应诗歌：", xml)
        self.assertIn("结束诗歌：", xml)
        self.assertIn(LINES[0], xml)
        check = d.renderer.inspect_docx(exported["path"])
        self.assertEqual(check["column_breaks"], 3)
        self.assertEqual(check["tables"], 3)
        sources = json.loads(Path(exported["sources"]).read_text())
        self.assertFalse(sources["rag_used"])
        self.assertFalse(sources["audio_downloaded"])
        self.assertFalse(d.status(self.state)["delivery_complete"])
        self.assertTrue(any(w["status"] == "proposed_structure" for w in exported["warnings"]))
        template = d.renderer.TemplateRenderer().template
        with zipfile.ZipFile(template) as original, zipfile.ZipFile(exported["path"]) as generated:
            for part in original.namelist():
                if part != "word/document.xml" and not part.startswith(("word/header", "word/footer")):
                    self.assertEqual(original.read(part), generated.read(part), part)

    def test_all_missing_still_exports_blank_content(self):
        self.state["materials"].clear()
        self.state["results"] = {m.identity(s): {"status": "missing"} for s in m.songs(self.state)}
        exported = self.output()
        self.assertFalse(any(v["lyrics"] for v in exported["features"].values()))
        self.assertNotIn(LINES[0], self.xml(exported))
        d.renderer.inspect_docx(exported["path"])
        self.assertFalse(d.status(self.state)["content_complete"])

    def test_export_cannot_skip_actual_resource_lookup(self):
        self.state["results"].clear()
        with self.assertRaisesRegex(WorkflowError, "Run download"):
            self.output()
        self.assertEqual(d.status(self.state)["next_action"], "download")

    def test_export_never_opens_a_database_or_full_workflow_modules(self):
        prepare(self.state)
        with patch("sqlite3.connect", side_effect=AssertionError("No database access allowed")), \
                patch.dict(sys.modules, {"rag": None, "keys": None, "setlist": None, "spoken": None}):
            exported = self.output()
        self.assertTrue(Path(exported["path"]).exists())

    def test_response_same_song_and_new_song_and_ending(self):
        base = {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)],
                "response": {"main_position": 2}, "ending": {"main_position": 3}}
        m.select(self.state, base, self.api)
        for sid in ("1", "2", "3"):
            prepare(self.state, sid)
        self.assertIn("结构同第2首", self.xml(self.output()))
        base["response"] = {"title": "测试歌曲4"}
        m.select(self.state, base, self.api)
        m.download(self.state, self.api, self.directory / "downloads")
        prepare(self.state, "4")
        exported = self.output("new-response.docx")
        self.assertTrue(exported["features"]["response"]["lyrics"])
        self.assertTrue(exported["features"]["ending"]["table"])
        self.assertEqual(self.xml(exported).count("调=  BPM="), 5)

    def test_user_scripture_and_spoken_only_with_unverified_warning(self):
        data = {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)],
                "spoken": [{"title": "用户提供话语", "text": "仅用于测试的话语",
                            "user_message": "Simulated user instruction"}]}
        data["main"][0]["scripture"] = {
            "reference": "测试章节", "translation": "测试译本", "quote": "测试引文",
            "source": "User-supplied fixture", "verification": "unverified",
            "user_message": "Simulated user instruction"}
        m.select(self.state, data, self.api)
        exported = self.output()
        self.assertIn("测试引文", self.xml(exported))
        self.assertIn("仅用于测试的话语", self.xml(exported))
        self.assertTrue(any(w["status"] == "scripture_unverified" for w in exported["warnings"]))

    def test_repeat_export_reused_but_existing_changed_file_never_overwritten(self):
        exported = self.output()
        self.assertTrue(self.output()["reused"])
        Path(exported["sources"]).write_text("User edit")
        with self.assertRaises(WorkflowError):
            self.output()
        self.assertEqual(Path(exported["sources"]).read_text(), "User edit")

    def test_changed_original_or_new_candidate_invalidates_export(self):
        prepare(self.state)
        exported = self.output()
        path = Path(self.state["results"]["1:11"]["path"])
        path.write_text("User edit")
        self.assertFalse(d.status(self.state)["export_valid"])
        with self.assertRaises(WorkflowError):
            d.finalize(self.state, acceptance(self.state, exported))
        with self.assertRaises(WorkflowError):
            self.output("changed.docx")

    def test_reselect_updated_candidate_requires_fresh_download(self):
        self.output()
        self.api.rows["1"]["updated_at"] = "new"
        m.select(self.state, {"main": [{"title": f"测试歌曲{i}"} for i in (1, 2, 3)]}, self.api)
        with self.assertRaises(WorkflowError):
            self.output("updated.docx")

    def test_native_acceptance_blank_lyrics_is_explicit_not_fake(self):
        exported = self.output()
        payload = acceptance(self.state, exported)
        payload["visual"]["songs"][0].pop("not_applicable")
        with self.assertRaises(WorkflowError):
            d.finalize(self.state, payload)
        self.assertFalse(d.status(self.state)["delivery_complete"])
        d.finalize(self.state, acceptance(self.state, exported))
        self.assertTrue(d.status(self.state)["delivery_complete"])
        self.assertEqual(d.status(self.state)["cleanup_remaining"], [])

    def test_cleanup_requires_exact_closed_hashes_and_preserves_user_edits(self):
        first = self.output()
        second = self.output("new.docx")
        payload = acceptance(self.state, second)
        payload["cleanup"]["checked_files"][0]["closed"] = False
        with self.assertRaises(WorkflowError):
            d.finalize(self.state, payload)
        self.assertTrue(Path(first["path"]).exists())
        payload = acceptance(self.state, second)
        Path(first["sources"]).write_text("User edit")
        with self.assertRaises(WorkflowError):
            d.finalize(self.state, payload)
        self.assertTrue(Path(first["path"]).exists())
        self.assertEqual(Path(first["sources"]).read_text(), "User edit")

    def test_cleanup_removes_only_registered_superseded_pair_and_preview(self):
        first = self.output()
        second = self.output("new.docx")
        preview = self.directory / "render-cache" / "page.png"
        preview.parent.mkdir()
        preview.write_bytes(b"fixture preview")
        d.register_preview(self.state, {"path": str(preview), "owner": second["path"]}, self.directory)
        unrelated = self.directory / "unrelated.docx"
        unrelated.write_bytes(b"User file")
        result = d.finalize(self.state, acceptance(self.state, second))
        self.assertTrue(result["complete"])
        self.assertEqual(set(result["deleted"]), {first["path"], first["sources"], str(preview)})
        self.assertTrue(unrelated.exists())
        self.assertTrue(Path(second["path"]).exists())
        self.assertTrue(Path(self.state["results"]["1:11"]["path"]).exists())

    def test_cleanup_partial_failure_audited_and_retryable(self):
        first = self.output()
        second = self.output("new.docx")
        payload = acceptance(self.state, second)
        unlink = Path.unlink
        def failure(path, *args, **kwargs):
            if str(path) == first["sources"]:
                raise OSError("Simulated failure")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", failure):
            result = d.finalize(self.state, payload)
        self.assertFalse(result["complete"])
        self.assertIn(first["path"], result["deleted"])
        self.assertFalse(d.status(self.state)["delivery_complete"])
        payload = acceptance(self.state, second)
        payload["use_existing_review"] = True
        self.assertTrue(d.finalize(self.state, payload)["complete"])

    def test_symlink_export_and_preview_rejected(self):
        target = self.directory / "target"
        target.mkdir()
        link = self.directory / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(WorkflowError):
            d.export(self.state, link / "out.docx")
        exported = self.output()
        with self.assertRaises(WorkflowError):
            d.register_preview(self.state, {"path": exported["path"], "owner": exported["path"]}, self.directory)

    def test_no_lyric_checks_can_be_waived_for_present_lyrics(self):
        prepare(self.state)
        exported = self.output()
        payload = acceptance(self.state, exported)
        payload["visual"]["songs"][0]["not_applicable"] = {"lyric_spacing": "not allowed"}
        with self.assertRaises(WorkflowError):
            d.finalize(self.state, payload)


class CLITests(unittest.TestCase):
    def test_real_cli_material_export_reuse_and_status_with_isolated_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            api, state = fixture()
            m.download(state, api, root / "downloads")
            material = prepare(state)["material"]
            material.pop("source_path")
            state["materials"].clear()
            path = root / "state.json"
            m.persist(path, state)
            def run(command, *args, payload=None):
                result = subprocess.run(
                    [sys.executable, "-B", str(m.ROOT / "mini.py"), "--state", str(path), command, *args],
                    text=True, input=json.dumps(payload) if payload is not None else None, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            run("material-prepare", payload=material)
            output = run("export", "--output", str(root / "cli.docx"))
            self.assertTrue(Path(output["sources"]).exists())
            self.assertTrue(run("export", "--output", str(root / "cli.docx"))["reused"])
            status = run("status")
            self.assertTrue(status["export_valid"])
            self.assertFalse(status["delivery_complete"])
            self.assertIsNone(status["selection"]["response"])

    def test_help_new_status_and_reject_full_workflow_state(self):
        with tempfile.TemporaryDirectory() as folder:
            def run(*args, **kwargs):
                return subprocess.run([sys.executable, "-B", str(m.ROOT / "mini.py"), *args],
                                      text=True, capture_output=True, **kwargs)
            self.assertEqual(run("--help").returncode, 0)
            result = run("new", "--runs-dir", folder)
            self.assertEqual(result.returncode, 0, result.stdout)
            path = json.loads(result.stdout)["state_path"]
            status = run("--state", path, "status")
            self.assertEqual(json.loads(status.stdout)["next_action"], "select")
            Path(path).write_text('{"schema_version":1,"stage":"confirmed"}')
            self.assertEqual(run("--state", path, "status").returncode, 2)


if __name__ == "__main__":
    unittest.main()
