"""Public, isolated adapter regressions; no PCO calls or private template required."""

import base64
import copy
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "workflow"))

from portable import runtime
from portable.bridge import Bridge
import credential_store
import engine
import file_lock
import guidance
import keys
import pco_api
from test_workflow import confirmed, prepared, proposal
from test_support import VOCALS, approve, inventory, proposal as key_proposal


class RuntimeTests(unittest.TestCase):
    def test_setup_requires_explicit_authorization(self):
        with patch.object(sys, "argv", ["reborn.py", "setup"]), patch.object(runtime, "setup") as install:
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(runtime.main(), 2)
        install.assert_not_called()
        self.assertIn("authorization_required", output.getvalue())

    def test_doctor_is_offline_and_never_installs(self):
        with patch.object(subprocess, "run") as run, patch.object(runtime, "TEMPLATE", Path("/missing/template.docx")):
            result = runtime.diagnose()
        run.assert_not_called()
        self.assertEqual(result["pco"]["status"], "not_checked")
        self.assertFalse(result["local_prerequisites_ready"])
        self.assertTrue(any(x["action"] == "import_authorized_template" for x in result["next_actions"]))

    def test_online_doctor_uses_api_not_cli_and_redacts(self):
        with patch.object(pco_api.PCO, "request", return_value={}) as request:
            result = runtime.diagnose(online=True)
        request.assert_called_once_with("/services/v2/songs?per_page=1")
        self.assertEqual(result["pco"]["status"], "verified")
        self.assertFalse(result["pco"]["credentials_logged"])

    def test_broken_or_missing_pdf_dependency_is_not_ready(self):
        original = runtime.importlib.import_module

        def load(name):
            if name == "pypdf":
                raise ImportError("simulated missing dependency")
            return original(name)

        with patch.object(runtime.importlib, "import_module", side_effect=load):
            result = runtime.diagnose()
        self.assertFalse(result["dependencies"]["pypdf"])
        self.assertTrue(any(item["action"] == "setup" for item in result["next_actions"]))

    def test_setup_is_private_and_preserves_existing_unknown_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, "ROOT", Path(directory)):
            path = Path(directory) / ".venv"
            path.mkdir()
            sentinel = path / "user-file"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaises(ValueError):
                runtime.setup()
            self.assertEqual(sentinel.read_text(), "preserve")

    def test_installer_uses_private_interpreter_and_fixed_requirements(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, "ROOT", Path(directory)):
            executable = runtime.python_path()
            executable.parent.mkdir(parents=True)
            executable.touch()
            (Path(directory) / ".venv/pyvenv.cfg").touch()
            with patch.object(subprocess, "run") as run, patch("sys.stdout", new_callable=io.StringIO):
                runtime.setup(with_mcp=True)
            self.assertEqual(run.call_args.args[0][0], str(executable))
            self.assertEqual(run.call_args.args[0][-1], str(Path(directory) / "requirements-agent.txt"))
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_library_initialization_does_not_invent_or_overwrite_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, "DATABASE", Path(directory) / "songs.sqlite3"):
            self.assertEqual(runtime.init_library()["historical_songs"], 0)
            before = runtime.DATABASE.read_bytes()
            self.assertTrue(runtime.init_library()["preserved_existing"])
            self.assertEqual(runtime.DATABASE.read_bytes(), before)

    def test_template_import_is_explicit_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            target = Path(directory) / "assets/reference.docx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("word/document.xml", "<document/>")
                archive.writestr("word/styles.xml", "<styles/>")
            with patch.object(runtime, "TEMPLATE", target):
                self.assertTrue(runtime.import_template(source)["imported"])
                before = target.read_bytes()
                with self.assertRaises(ValueError):
                    runtime.import_template(source)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(source.read_bytes(), before)

    def test_auth_refuses_agent_stdin(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(ValueError):
                runtime.configure_auth()

    def test_system_install_is_scoped_and_no_automatic_elevation(self):
        with patch("platform.system", return_value="Windows"), patch("shutil.which", side_effect=lambda n: n if n == "winget" else None):
            with patch("subprocess.run") as run:
                runtime.install_system_tools()
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["winget", "install", "--id", "Gyan.FFmpeg"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertNotIn("sudo", command)

    @unittest.skipIf(os.name == "nt", "Legacy POSIX shell entrypoints; Windows uses reborn.py")
    def test_partial_virtualenv_does_not_break_original_shell_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            local = root / ".venv/bin/python"
            old = home / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
            for path in (local, old):
                path.parent.mkdir(parents=True, exist_ok=True)
            local.write_text("#!/bin/sh\nexit 1\n")
            old.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "legacy-fallback\\n"\n')
            for path in (local, old):
                path.chmod(0o755)
            env = dict(os.environ, HOME=str(home))
            for name in ("WORKFLOW_PYTHON", "MINI_WORKFLOW_PYTHON", "RAG_PYTHON"):
                env.pop(name, None)
            for mode in ("workflow", "mini-workflow", "rag"):
                target = root / mode / "run"
                target.parent.mkdir()
                shutil.copy2(ROOT / mode / "run", target)
                result = subprocess.run(["sh", str(target), "--help"], env=env,
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "legacy-fallback")


class CredentialTests(unittest.TestCase):
    def test_environment_precedence_is_unchanged(self):
        with patch.dict(os.environ, {"PCO_CLIENT_ID": "test-id", "PCO_SECRET": "test-secret"}):
            with patch.object(credential_store, "load") as load:
                self.assertEqual(pco_api.credentials(), base64.b64encode(b"test-id:test-secret").decode())
        load.assert_not_called()

    def test_partial_environment_cannot_fall_back(self):
        with patch.dict(os.environ, {"PCO_CLIENT_ID": "test-id", "PCO_SECRET": ""}):
            with patch.object(credential_store, "load") as load, self.assertRaises(pco_api.WorkflowError):
                pco_api.credentials()
        load.assert_not_called()

    def test_os_store_is_supported_without_pco_cli(self):
        with patch.dict(os.environ, {"PCO_CLIENT_ID": "", "PCO_SECRET": ""}):
            with patch.object(credential_store, "load", return_value=["id", "secret"]), patch("subprocess.run") as run:
                self.assertEqual(pco_api.credentials(), base64.b64encode(b"id:secret").decode())
        run.assert_not_called()

    def test_partial_os_store_never_mixes_with_legacy_credentials(self):
        with patch.dict(os.environ, {"PCO_CLIENT_ID": "", "PCO_SECRET": ""}):
            with patch.object(credential_store, "load", return_value=["id", None]):
                with patch("subprocess.run") as run, self.assertRaises(pco_api.WorkflowError):
                    pco_api.credentials()
        run.assert_not_called()

    def test_plaintext_or_unknown_keyring_backend_is_refused(self):
        fake = types.SimpleNamespace(get_keyring=lambda: Mock())
        with patch.dict(sys.modules, {"keyring": fake}), self.assertRaises(RuntimeError):
            credential_store.backend()

    def test_original_mac_keychain_fallback_remains(self):
        with patch.dict(os.environ, {"PCO_CLIENT_ID": "", "PCO_SECRET": ""}):
            with patch.object(credential_store, "load", return_value=[None, None]):
                with patch("subprocess.run", side_effect=[Mock(stdout="old-id"), Mock(stdout="old-secret")]) as run:
                    self.assertEqual(pco_api.credentials(), base64.b64encode(b"old-id:old-secret").decode())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][:3], ["security", "find-generic-password", "-s"])

    def test_os_store_failure_does_not_write_plaintext(self):
        with patch.object(credential_store, "backend", side_effect=RuntimeError("locked")):
            self.assertEqual(credential_store.load(), [None, None])
            with self.assertRaises(RuntimeError):
                credential_store.save("id", "secret")

    def test_failed_pair_write_rolls_back_previous_pair(self):
        store = Mock()
        store.get_password.side_effect = ["old-id", "old-secret"]
        store.set_password.side_effect = [None, RuntimeError("simulated failure"), None, None]
        with patch.object(credential_store, "backend", return_value=store):
            with self.assertRaises(RuntimeError):
                credential_store.save("new-id", "new-secret")
        self.assertEqual(store.set_password.call_args_list[-2].args[-1], "old-id")
        self.assertEqual(store.set_password.call_args_list[-1].args[-1], "old-secret")


class LockTests(unittest.TestCase):
    def test_actual_processes_serialize_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "counter").write_text("0")
            code = (
                "import sys,time; from pathlib import Path; "
                f"sys.path.insert(0, {str(ROOT / 'workflow')!r}); "
                "from file_lock import exclusive; "
                "p=Path(sys.argv[1]); "
                "h=(p/'lock').open('a+'); "
                "ctx=exclusive(h); ctx.__enter__(); "
                "n=int((p/'counter').read_text()); time.sleep(.2); "
                "(p/'counter').write_text(str(n+1)); ctx.__exit__(None,None,None); h.close()"
            )
            processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(root)]) for _ in range(3)]
            for process in processes:
                self.assertEqual(process.wait(timeout=30), 0)
            self.assertEqual((root / "counter").read_text(), "3")

    def test_windows_lock_retries_busy_byte_and_releases_on_error(self):
        module = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2,
                                       locking=Mock(side_effect=[OSError(errno.EACCES, "busy"), None, None]))
        with tempfile.TemporaryFile(mode="w+") as handle:
            with patch.dict(sys.modules, {"msvcrt": module}), patch.object(file_lock.os, "name", "nt"):
                with patch.object(file_lock.time, "sleep") as sleep:
                    with self.assertRaisesRegex(ValueError, "inside"):
                        with file_lock.exclusive(handle):
                            raise ValueError("inside")
                sleep.assert_called_once()
            self.assertEqual(module.locking.call_args.args[1:], (2, 1))
            handle.seek(0)
            self.assertEqual(handle.read(), "0")


class GuidanceTests(unittest.TestCase):
    def test_initial_and_partial_requirements_are_read_only(self):
        state = engine.new_state()
        original = copy.deepcopy(state)
        self.assertEqual(guidance.next_step(state)["missing"], ["theme", "slots", "preferences"])
        self.assertEqual(state, original)
        engine.requirements(state, {"theme": "test"})
        self.assertEqual(guidance.next_step(state)["missing"], ["slots", "preferences"])

    def test_research_song_confirmation_and_singers(self):
        state = prepared()
        self.assertEqual(guidance.next_step(state)["action"], "research_and_propose")
        engine.propose(state, proposal())
        result = guidance.next_step(state)
        self.assertEqual(result["actor"], "user")
        self.assertFalse(result["automatic_confirmation_allowed"])
        state = confirmed()
        self.assertEqual(guidance.next_step(state)["action"], "collect_vocals")
        keys.requirements(state, VOCALS)
        self.assertEqual(guidance.next_step(state)["action"], "propose_keys")
        with patch("keys.inventory", side_effect=inventory):
            keys.refresh(state, Mock())
        keys.propose(state, key_proposal())
        self.assertEqual(guidance.next_step(state)["action"], "prepare_audition")

    def test_audition_files_do_not_create_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = approve(confirmed(), Path(directory))
            state["keys"]["confirmation"] = None
            state["keys"]["stage"] = "awaiting_audition"
            result = guidance.next_step(state)
            self.assertEqual(result["action"], "listen_and_confirm_keys")
            self.assertIsNone(state["keys"]["confirmation"])
            # A changed or missing file requires preparation, never human approval.
            Path(next(iter(state["keys"]["previews"]["K1"].values()))["path"]).unlink()
            self.assertEqual(guidance.next_step(state)["action"], "prepare_audition")

    def test_missing_download_and_materials_remain_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            state = approve(confirmed(), Path(directory))
            self.assertEqual(guidance.next_step(state)["action"], "prepare_and_download_resources")
            for sid, preview in state["keys"]["previews"]["K1"].items():
                state["results"][sid] = {"status": "missing", "audio": {**preview, "status": "downloaded"}}
            self.assertEqual(guidance.next_step(state)["action"], "prepare_materials")
            state["results"]["1"]["status"] = "restricted"
            self.assertEqual(guidance.next_step(state)["missing"], ["1"])

    def test_later_stages_still_require_real_approvals_and_native_review(self):
        state = confirmed()
        state["keys"] = {"previews": {"K": {}}}
        state["spoken"] = {"stage": "awaiting_confirmation"}
        # Tail-state hints are tested independently of source/resource validators.
        with patch.object(keys, "confirmed", return_value={"id": "K", "songs": []}):
            with patch.object(guidance.setlist, "material", return_value=({"test": True}, {})):
                self.assertEqual(guidance.next_step(state)["action"], "confirm_spoken")
                with patch.object(guidance.spoken, "render", return_value="verified draft"):
                    self.assertEqual(guidance.next_step(state)["action"], "prepare_setlist")
                    state["setlist"] = {"stage": "awaiting_confirmation"}
                    with patch.object(guidance.setlist, "current", return_value={"test": True}):
                        self.assertEqual(guidance.next_step(state)["action"], "review_and_confirm_setlist")
                        state["setlist"]["stage"] = "confirmed"
                        with patch.object(guidance.status, "summary") as summary:
                            summary.return_value = {"delivery_complete": False, "current_artifact": None}
                            self.assertEqual(guidance.next_step(state)["action"], "export_setlist")
                            summary.return_value = {"delivery_complete": False, "current_artifact": {"test": True},
                                                    "artifact_bytes_valid": True}
                            self.assertEqual(guidance.next_step(state)["actor"], "capable_reviewer")
                            summary.return_value["artifact_bytes_valid"] = False
                            self.assertEqual(guidance.next_step(state)["action"], "repair_artifact")
                            summary.return_value = {"delivery_complete": True}
                            self.assertEqual(guidance.next_step(state)["action"], "complete")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "workflow/runs").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "workflow/runs", prefix="adapter-test-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = Bridge()
        self.bridge.runs = Path(self.temp.name)

    def test_portable_entrypoint_preserves_stdin_and_unicode_paths(self):
        directory = Path(self.temp.name) / "中文 歌单"
        command = [sys.executable, "-B", str(ROOT / "reborn.py"), "run", "workflow"]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run(command + ["new", "--runs-dir", str(directory), "--test-run"],
                                capture_output=True, text=True, encoding="utf-8", env=env, check=True)
        state = json.loads(result.stdout)["state_path"]
        payload = {"theme": "隔离测试主题", "slots": ["slow"] * 3, "preferences": []}
        result = subprocess.run(command + ["--state", state, "requirements"],
                                input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                                text=True, encoding="utf-8", env=env, check=True)
        self.assertEqual(json.loads(result.stdout)["requirements"]["theme"], payload["theme"])
        self.assertEqual(Path(state).parent.parent, directory)

    def test_actual_cli_new_requirements_next_and_rejected_bypass(self):
        result = self.bridge.execute("new", test_run=True)
        self.assertTrue(result["ok"], result)
        state = result["result"]["state_path"]
        result = self.bridge.execute("requirements", state, {
            "theme": "isolated adapter test", "slots": ["slow"] * 3, "preferences": []})
        self.assertTrue(result["ok"], result)
        result = self.bridge.execute("next", state)
        self.assertEqual(result["result"]["action"], "research_and_propose")
        before = Path(state).read_bytes()
        result = self.bridge.execute("key-confirm", state, {
            "confirmed": True, "listened": True, "user_message": "AUTOMATION ONLY invalid bypass"})
        self.assertFalse(result["ok"])
        self.assertEqual(Path(state).read_bytes(), before)

    def test_invalid_paths_commands_and_options_are_rejected(self):
        for name in ("cleanup", "bash", "auth", "--help"):
            with self.assertRaises(ValueError):
                self.bridge.execute(name)
        with self.assertRaises(ValueError):
            self.bridge.guide("../SECURITY.md")
        with self.assertRaises(ValueError):
            self.bridge.rag("ingest", "anything")
        with self.assertRaises(ValueError):
            self.bridge.rag("context", "--help")
        outside = Path(self.temp.name) / "outside/state.json"
        outside.parent.mkdir()
        outside.write_text("{}")
        self.bridge.runs = outside.parent / "other"
        with self.assertRaises(ValueError):
            self.bridge.execute("show", str(outside))

    def test_credentials_in_payload_are_rejected_by_original_engine(self):
        state = self.bridge.execute("new", test_run=True)["result"]["state_path"]
        before = Path(state).read_bytes()
        response = self.bridge.execute("requirements", state, {"client_secret": "simulated-not-real"})
        self.assertFalse(response["ok"])
        self.assertNotIn("simulated-not-real", json.dumps(response))
        self.assertEqual(Path(state).read_bytes(), before)

    def test_export_target_is_managed_and_never_shell_interpolated(self):
        state = self.bridge.execute("new", test_run=True)["result"]["state_path"]
        with patch.object(self.bridge, "run", return_value={"ok": True}) as run:
            self.bridge.execute("setlist-export", state, draft=True)
        arguments = run.call_args.args[1]
        self.assertIn(str(Path(state).parent / "delivery/draft.docx"), arguments)
        self.assertIn("--draft", arguments)
        self.assertIn("--reuse-existing", arguments)
        with self.assertRaises(ValueError):
            self.bridge.execute("inspect", state, song_ids=["1; echo bad"])

    def test_resource_access_cannot_read_credentials_or_external_files(self):
        token = base64.urlsafe_b64encode(b".env").decode()
        with self.assertRaises(ValueError):
            self.bridge.asset_path(token)
        token = base64.urlsafe_b64encode(b"../outside.mp3").decode()
        with self.assertRaises(ValueError):
            self.bridge.asset_path(token)

    def test_managed_runs_cannot_be_redirected_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            self.bridge.runs = Path(directory)
            with self.assertRaises(ValueError):
                self.bridge.execute("new", test_run=True)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_error_logs_are_not_returned_to_agent(self):
        result = Mock(returncode=1, stdout="", stderr="simulated-secret-must-not-leak")
        with patch("subprocess.run", return_value=result):
            response = self.bridge.run("workflow/workflow.py", ["status"])
        self.assertNotIn("simulated-secret", json.dumps(response))
        self.assertFalse(response["ok"])


if __name__ == "__main__":
    unittest.main()
