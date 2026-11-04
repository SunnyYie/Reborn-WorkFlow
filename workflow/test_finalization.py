"""Mandatory single-final-output lifecycle, using isolated files and simulated observations."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import artifacts
from pco_api import WorkflowError
import setlist as sl
import status
import test_setlist as fixtures
import test_quality_gates
import workflow


class FinalizationTests(unittest.TestCase):
    setUp = fixtures.SetlistTests.setUp
    ready = fixtures.SetlistTests.ready
    downloaded_material = fixtures.SetlistTests.downloaded_material
    review_payload = test_quality_gates.QualityGateTests.review_payload

    def formal(self, name="final.docx"):
        if not self.state.get("setlist", {}).get("selection"):
            sl.propose(self.state, self.ready())
        if not self.state["setlist"].get("confirmation"):
            sl.confirm(self.state, {"revision": self.state["setlist"]["revision"],
                                   "confirmed": True, "user_message": "AUTOMATION ONLY"})
        return sl.export(self.state, self.root / name)

    def payload(self, result):
        rows = [row for row in self.state["artifacts"]
                if not row.get("deleted_at") and row["path"] not in {result["path"], result["sources"]}
                and row["kind"] in {"final", "sidecar", "temporary", "obsolete"}]
        return self.review_payload(result) | {"cleanup": {
            "user_message": "AUTOMATION ONLY simulated closed-file observations",
            "checked_files": [{"path": row["path"], "sha256": row["sha256"], "closed": True}
                              for row in rows]}}

    def old_and_final(self):
        sl.propose(self.state, self.ready())
        old = sl.export(self.state, self.root / "old.docx", draft=True)
        return old, self.formal()

    def test_combined_review_retire_cleanup_keeps_exactly_one_pair_and_originals(self):
        old, final = self.old_and_final()
        preview = self.root / "preview.png"
        preview.write_bytes(b"generated preview")
        artifacts.register(self.state, preview, "temporary", old["path"])
        untouched = self.root / "user.docx"
        untouched.write_bytes(b"unregistered user file")
        content = Path(final["path"]).read_bytes()
        result = artifacts.finalize(self.state, self.payload(final))
        self.assertTrue(result["complete"])
        self.assertEqual(set(result["deleted"]), {old["path"], old["sources"], str(preview.resolve())})
        self.assertEqual(Path(final["path"]).read_bytes(), content)
        self.assertTrue(Path(final["sources"]).exists())
        self.assertTrue(self.source.exists())
        self.assertTrue(untouched.exists())
        self.assertTrue(artifacts.finalized(self.state, final["path"]))
        self.assertTrue(self.state["setlist"]["exports"][0]["retired_for_cleanup"])

    def test_review_alone_or_manual_cleanup_cannot_bypass_required_finalization(self):
        self.state["test_run"] = False
        final = self.formal()
        artifacts.review(self.state, self.review_payload(final))
        artifacts.cleanup(self.state, {"apply": True})
        current = status.summary(self.state)
        self.assertTrue(current["visual_review_valid"])
        self.assertTrue(current["finalization_required"])
        self.assertFalse(current["finalization_valid"])
        self.assertFalse(current["delivery_complete"])
        self.assertEqual(current["next_action"], "artifact-finalize")
        artifacts.finalize(self.state, self.payload(final))
        self.assertTrue(status.summary(self.state)["delivery_complete"])

    def test_accepted_formal_export_blocks_further_exports_until_finalized(self):
        final = self.formal()
        artifacts.review(self.state, self.review_payload(final))
        for draft in (True, False):
            target = self.root / f"blocked-{draft}.docx"
            with self.assertRaisesRegex(WorkflowError, "artifact-finalize"):
                sl.export(self.state, target, draft=draft)
            self.assertFalse(target.exists())
        artifacts.finalize(self.state, self.payload(final))
        newer = sl.export(self.state, self.root / "newer.docx")
        self.assertTrue(Path(newer["path"]).exists())
        # Old final remains until its replacement passes the combined operation.
        self.assertTrue(Path(final["path"]).exists())
        artifacts.finalize(self.state, self.payload(newer))
        self.assertFalse(Path(final["path"]).exists())

    def test_missing_open_unbound_and_invented_inspections_reject_without_mutation(self):
        old, final = self.old_and_final()
        for change in ("missing", "omitted", "open", "string", "hash", "extra"):
            payload = self.payload(final)
            checks = payload["cleanup"]["checked_files"]
            if change == "missing":
                del payload["cleanup"]
            elif change == "omitted":
                checks.pop()
            elif change == "open":
                checks[0]["closed"] = False
            elif change == "string":
                checks[0]["closed"] = "true"
            elif change == "hash":
                checks[0]["sha256"] = "wrong"
            else:
                checks.append({"path": str(self.source), "sha256": "invented", "closed": True})
            before = copy.deepcopy(self.state)
            with self.subTest(change=change), self.assertRaises(WorkflowError):
                artifacts.finalize(self.state, payload)
            self.assertEqual(self.state, before)
            self.assertTrue(Path(old["path"]).exists())
            self.assertTrue(Path(old["sources"]).exists())

    def test_edited_docx_or_sources_preserved_and_entire_batch_blocked(self):
        old, final = self.old_and_final()
        for field in ("path", "sources"):
            with self.subTest(field=field):
                path = Path(old[field])
                original = path.read_bytes()
                payload = self.payload(final)
                path.write_bytes(b"user changes")
                before = copy.deepcopy(self.state)
                with self.assertRaises(WorkflowError):
                    artifacts.finalize(self.state, payload)
                self.assertEqual(self.state, before)
                self.assertTrue(Path(old["path"]).exists())
                self.assertTrue(Path(old["sources"]).exists())
                self.assertEqual(path.read_bytes(), b"user changes")
                path.write_bytes(original)

    def test_failed_visual_acceptance_preserves_previous_export(self):
        old, final = self.old_and_final()
        payload = self.payload(final)
        payload["visual"]["songs"][0]["checks"].remove("section_spacing")
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, payload)
        self.assertTrue(Path(old["path"]).exists())
        self.assertNotIn("artifact_reviews", self.state)

    def test_draft_and_nonlatest_cannot_delete_confirmed_final(self):
        old, final = self.old_and_final()
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, self.payload(old))
        draft = sl.export(self.state, self.root / "draft.docx", draft=True)
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, self.payload(draft))
        self.assertTrue(Path(final["path"]).exists())

    def test_stale_confirmation_cannot_finalize_or_remove_old_export(self):
        old, final = self.old_and_final()
        self.state["setlist"]["confirmation"]["selection_digest"] = "stale"
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, self.payload(final))
        self.assertTrue(Path(old["path"]).exists())

    def test_symlink_or_protected_preview_blocks_entire_batch(self):
        old, final = self.old_and_final()
        temp = self.root / "preview.png"
        temp.write_bytes(b"generated")
        artifacts.register(self.state, temp, "temporary", final["path"])
        self.state["user_reference"] = str(temp)
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, self.payload(final))
        self.assertTrue(Path(old["path"]).exists())
        del self.state["user_reference"]
        temp.unlink()
        temp.symlink_to(self.source)
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, self.payload(final))
        self.assertTrue(self.source.exists())
        self.assertTrue(Path(old["path"]).exists())

    def test_partial_io_failure_is_audited_and_retryable_not_complete(self):
        old, final = self.old_and_final()
        payload = self.payload(final)
        unlink = Path.unlink
        def fail_sources(path, *args, **kwargs):
            if str(path) == old["sources"]:
                raise PermissionError("isolated failure")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_sources):
            result = artifacts.finalize(self.state, payload)
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"], "cleanup_io_error")
        self.assertEqual(result["deleted"], [old["path"]])
        self.assertFalse(artifacts.finalized(self.state, final["path"]))
        result = artifacts.finalize(self.state, self.payload(final))
        self.assertTrue(result["complete"])
        self.assertFalse(Path(old["sources"]).exists())

    def test_idempotent_empty_finalization_and_new_preview_invalidates_it(self):
        final = self.formal()
        for _ in range(2):
            result = artifacts.finalize(self.state, self.payload(final))
            self.assertTrue(result["complete"])
            self.assertEqual(result["deleted"], [])
        temp = self.root / "later-preview.png"
        temp.write_bytes(b"preview")
        artifacts.register(self.state, temp, "temporary", final["path"])
        self.assertFalse(artifacts.finalized(self.state, final["path"]))

    def test_new_drafts_can_be_refined_after_previous_formal_was_finalized(self):
        final = self.formal()
        artifacts.finalize(self.state, self.payload(final))
        for i in (1, 2):
            draft = sl.export(self.state, self.root / f"draft-{i}.docx", draft=True)
            artifacts.review(self.state, self.review_payload(draft))
        newer = sl.export(self.state, self.root / "new-formal.docx")
        artifacts.finalize(self.state, self.payload(newer))
        self.assertFalse(Path(final["path"]).exists())
        self.assertFalse(Path(draft["path"]).exists())

    def test_prior_revision_exports_are_retired_without_touching_history_evidence(self):
        old, _ = self.old_and_final()
        sl.propose(self.state, {
            "main": [{"song_id": str(i), "scripture": copy.deepcopy(fixtures.VERSE)} for i in (1, 2, 3)],
            "response": {"song_id": "1"}, "ending": {"song_id": "3"}})
        final = self.formal("new-revision.docx")
        artifacts.finalize(self.state, self.payload(final))
        self.assertFalse(Path(old["path"]).exists())
        self.assertTrue(self.state["setlist"]["history"])

    def test_existing_native_review_reused_without_changing_its_observation_timestamp(self):
        old, final = self.old_and_final()
        artifacts.review(self.state, self.review_payload(final))
        before = copy.deepcopy(self.state["artifact_reviews"])
        payload = self.payload(final)
        del payload["visual"]
        payload["use_existing_review"] = True
        artifacts.finalize(self.state, payload)
        self.assertEqual(self.state["artifact_reviews"], before)
        self.assertFalse(Path(old["path"]).exists())

    def test_existing_review_reuse_cannot_skip_review_or_accept_changed_current_file(self):
        old, final = self.old_and_final()
        payload = self.payload(final) | {"use_existing_review": True}
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, payload)
        artifacts.review(self.state, self.review_payload(final))
        Path(final["path"]).write_bytes(b"changed final")
        with self.assertRaises(WorkflowError):
            artifacts.finalize(self.state, payload)
        self.assertTrue(Path(old["path"]).exists())

    def test_cli_finalization_persists_cleanup_and_bad_input_preserves_state(self):
        old, final = self.old_and_final()
        path = self.root / "state.json"
        workflow.persist(path, self.state)
        payload = self.payload(final)
        def call(data):
            output = io.StringIO()
            with patch.object(sys, "argv", ["workflow.py", "--state", str(path), "artifact-finalize"]), \
                    patch.object(sys, "stdin", io.StringIO(json.dumps(data))), \
                    patch.object(sys, "stdout", output):
                workflow.main()
            return json.loads(output.getvalue())
        bad = copy.deepcopy(payload)
        bad["cleanup"]["checked_files"] = []
        before = path.read_bytes()
        with self.assertRaises(WorkflowError):
            call(bad)
        self.assertEqual(path.read_bytes(), before)
        result = call(payload)
        self.assertTrue(result["complete"])
        saved = json.loads(path.read_text())
        self.assertTrue(artifacts.finalized(saved, final["path"]))
        self.assertFalse(Path(old["path"]).exists())

    def test_cli_io_failure_exits_nonzero_and_persists_partial_audit(self):
        old, final = self.old_and_final()
        path = self.root / "state.json"
        workflow.persist(path, self.state)
        output = io.StringIO()
        unlink = Path.unlink
        def fail_sources(candidate, *args, **kwargs):
            if str(candidate) == old["sources"]:
                raise PermissionError("isolated failure")
            return unlink(candidate, *args, **kwargs)
        with patch.object(sys, "argv", ["workflow.py", "--state", str(path), "artifact-finalize"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(self.payload(final)))), \
                patch.object(sys, "stdout", output), patch.object(Path, "unlink", fail_sources), \
                self.assertRaises(SystemExit) as stopped:
            workflow.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertFalse(json.loads(output.getvalue())["complete"])
        saved = json.loads(path.read_text())
        self.assertFalse(artifacts.finalized(saved, final["path"]))
        self.assertEqual(saved["cleanup_history"][-1]["deleted"], [old["path"]])


if __name__ == "__main__":
    unittest.main()
