import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import check_repository as policy


class RepositoryPolicyTests(unittest.TestCase):
    def test_rejects_private_materials_and_nested_caches(self):
        for path in (
            "workflow/runs/session/state.json", "rag/data/songs.sqlite3", "rag/catalog.json",
            "memory/notes.md", "demo/example.txt", "rag/__pycache__/rag.cpython-312.pyc",
            "a/.DS_Store", "a/._file", ".env", ".env.production", "a/.env.local",
            ".aws/credentials", ".codex/config.toml", ".agents/state.json",
            "a/client_secret_local.json", "credentials.json", "a/id.key", "a/server.pem",
            "a/song.MP3", "a/document.DOCX", "a/score.pdf", "a/session.sqlite3-wal",
            "a/session.db-shm", "a/.pytest_cache/cache", "a/output.log",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(policy.reason(path))

    def test_preserves_source_and_nonprivate_examples(self):
        for path in (
            ".env.example", ".github/workflows/repository-hygiene.yml",
            "workflow/pco_api.py", "rag/catalog.example.json",
            "workflow/quality-optimization.md", "Agent.md", "SECURITY.md",
        ):
            with self.subTest(path=path):
                self.assertIsNone(policy.reason(path))

    def test_index_tree_and_history_are_separate_and_do_not_read_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", "-C", directory, *args], check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                                           "GIT_CONFIG_NOSYSTEM": "1"})
            git("init", "-q")
            git("config", "user.name", "Isolated Test")
            git("config", "user.email", "test@example.invalid")
            private = root / "workflow/runs/space name/state.json"
            private.parent.mkdir(parents=True)
            private.write_text('{"fixture":true}')
            git("add", "--", "workflow")
            git("commit", "-qm", "isolated private fixture")
            private.unlink()
            git("add", "-u")
            (root / "safe.py").write_text("pass\n")
            git("add", "--", "safe.py")
            # A local untracked secret must not be read or printed by a source-index check.
            (root / ".env").write_text("LOCAL_ONLY=fixture\n")
            previous = os.getcwd()
            try:
                os.chdir(directory)
                self.assertEqual(policy.inspect(), [])
                self.assertEqual(len(policy.inspect("HEAD")), 1)
                self.assertEqual(len(policy.inspect(history=True)), 1)
            finally:
                os.chdir(previous)
            git("commit", "-qm", "remove fixture from current tree")
            try:
                os.chdir(directory)
                self.assertEqual(policy.inspect("HEAD"), [])
                self.assertEqual(len(policy.inspect(history=True)), 1)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
