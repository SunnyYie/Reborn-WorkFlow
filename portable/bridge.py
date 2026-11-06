"""Bounded tool adapter. All mutations still go through the original CLI."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
RESOURCE_TYPES = {
    "audio": "audio/mpeg",
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg",
    "sources": "application/json", "text": "text/plain",
}


def resource_kind(path):
    if path.name.endswith(".sources.json"):
        return "sources"
    return {".mp3": "audio", ".docx": "word", ".pdf": "pdf", ".png": "png",
            ".jpg": "jpeg", ".jpeg": "jpeg", ".md": "text", ".txt": "text"}.get(path.suffix.lower())


INPUT_COMMANDS = frozenset({
    "requirements", "propose", "feedback", "confirm", "review-resources",
    "key-requirements", "key-propose", "key-feedback", "key-confirm", "key-preview",
    "spoken-requirements", "spoken-propose", "spoken-feedback", "spoken-confirm",
    "material-prepare", "setlist-propose", "setlist-confirm", "artifact-finalize",
    "response-options", "response-preview", "response-confirm", "response-resources",
})
READ_COMMANDS = frozenset({"show", "status", "next", "spoken-context", "key-options", "resources"})
COMMANDS = INPUT_COMMANDS | READ_COMMANDS | {
    "new", "catalog", "inspect", "rag-search", "download", "spoken-export", "setlist-export"}
GUIDES = {
    "Agent.md", "workflow/SKILL.md", "workflow/README.md", "rag/SKILL.md", "rag/README.md",
    "mini-workflow/SKILL.md", "mini-workflow/README.md",
    *(str(p.relative_to(ROOT)).replace(os.sep, "/") for p in (ROOT / "skills").rglob("*.md")),
}


class Bridge:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.runs = self.root / "workflow/runs"

    def runs_path(self):
        path = self.runs.resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Managed runs cannot redirect outside this checkout.")
        return path

    def state(self, value):
        path = Path(value)
        if path.is_symlink() or not path.is_file() or path.name != "state.json":
            raise ValueError("Choose an existing managed state.json.")
        path = path.resolve()
        if not path.is_relative_to(self.runs_path()):
            raise ValueError("MCP state must belong to this checkout's workflow/runs.")
        return path

    def run(self, script, arguments, payload=None):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(self.root / script), *arguments],
                input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                capture_output=True, text=True, encoding="utf-8", env=env,
                cwd=self.root, timeout=600,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout",
                    "message": "Inspect status before retrying; the operation may have partially completed."}
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = {"error": "engine_failed",
                    "message": "Engine returned no JSON; run environment diagnostics. Raw logs are not exposed."}
        return {"ok": result.returncode == 0, "exit_code": result.returncode, "result": data}

    def execute(self, command, state_path=None, payload=None, query=None, song_ids=None,
                title=None, draft=False, test_run=False):
        if command not in COMMANDS:
            raise ValueError("Unsupported command; arbitrary shell commands are not accepted.")
        if payload is not None and command not in INPUT_COMMANDS:
            raise ValueError("This command does not accept a payload.")
        if command in INPUT_COMMANDS and not isinstance(payload, dict):
            raise ValueError("This command needs a JSON object payload.")
        args = []
        state = None
        if command not in {"new", "catalog"}:
            state = self.state(state_path or "")
            args += ["--state", str(state)]
        args.append(command)
        if command == "new":
            args += ["--runs-dir", str(self.runs_path())]
            if test_run:
                args.append("--test-run")
        elif command == "catalog" and query:
            args += ["--query", query]
        elif command == "inspect":
            if not song_ids or any(not str(s).isascii() or not str(s).isdigit() for s in song_ids):
                raise ValueError("Provide real decimal PCO song IDs.")
            args += [str(s) for s in song_ids]
        elif command == "rag-search":
            if not isinstance(title, str) or not title.strip() or title.startswith("-"):
                raise ValueError("Provide a song title.")
            args += [title]
        elif command == "setlist-export":
            directory = state.parent / "delivery"
            if directory.is_symlink():
                raise ValueError("Delivery cannot redirect outside the managed session.")
            args += ["--output", str(directory / ("draft.docx" if draft else "setlist.docx")),
                     "--reuse-existing"]
            if draft:
                args.append("--draft")
        return self.run("workflow/workflow.py", args, payload)

    def rag(self, command, title_or_id):
        if command not in {"resolve", "context", "ending", "search"}:
            raise ValueError("Only read-only RAG commands are exposed.")
        if not isinstance(title_or_id, str) or not title_or_id or title_or_id.startswith("-"):
            raise ValueError("Provide a title or record ID.")
        return self.run("rag/rag.py", [command, title_or_id])

    def guide(self, name="Agent.md"):
        if name not in GUIDES:
            raise ValueError("Choose a published guide from the guide list.")
        return (self.root / name).read_text(encoding="utf-8")

    def asset_path(self, token):
        try:
            relative = base64.urlsafe_b64decode(token.encode()).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("Invalid asset token.") from None
        path = self.root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("Asset is missing or not a regular file.")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.runs_path()):
            raise ValueError("Assets must belong to managed sessions.")
        if not resource_kind(path):
            raise ValueError("This file type is not exposed as a resource.")
        if path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("Asset exceeds the 30 MiB resource limit.")
        return resolved

    def assets(self, state_path):
        state = self.state(state_path)
        # Only files referenced by the engine, never arbitrary directory contents.
        response = self.run("workflow/workflow.py", ["--state", str(state), "show"])
        if not response["ok"]:
            return response
        found = {}

        def visit(value):
            if isinstance(value, dict):
                if isinstance(value.get("path"), str):
                    path = Path(value["path"])
                    try:
                        relative = str(path.resolve().relative_to(self.root))
                        token = base64.urlsafe_b64encode(relative.encode("utf-8")).decode()
                        asset = self.asset_path(token)
                        kind = resource_kind(asset)
                        found[str(asset)] = {
                            "path": str(asset), "uri": "reborn://" + kind + "/" + token,
                            "mime_type": RESOURCE_TYPES[kind],
                        }
                    except (OSError, ValueError):
                        pass
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(response["result"])
        return {"assets": list(found.values()),
                "notice": "Playback/preview support depends on the host. Listing audio is not human listening."}
