#!/usr/bin/env python3
"""JSON command interface used by the Codex conversation workflow."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import downloads
import engine
import keys
import spoken
import setlist
import artifacts
import supplement
import status
from pco_api import PCO, WorkflowError

ROOT = Path(__file__).resolve().parent


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def read_input(path):
    try:
        value = json.load(sys.stdin if path == "-" else open(path, encoding="utf-8"))
    except (OSError, ValueError):
        raise WorkflowError("invalid_input", "Cannot read JSON input.") from None
    engine.require(isinstance(value, dict), "JSON input must be an object.")
    engine.safe_input(value)
    return value


def persist(path, state):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path):
    engine.require(path.is_file(), "Session state does not exist.")
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        engine.require(state.get("schema_version") == 1, "Unsupported state version.")
        yield state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, help="Session state.json path")
    parser.add_argument("--rag-db", type=Path, default=setlist.DEFAULT_DB)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new")
    new.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    new.add_argument("--test-run", action="store_true", help="Isolated automation; exports are not live approvals")
    commands.add_parser("show")
    commands.add_parser("status")
    commands.add_parser("spoken-context")
    commands.add_parser("doctor")
    catalog = commands.add_parser("catalog")
    catalog.add_argument("--query")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("song_ids", nargs="+")
    query = commands.add_parser("rag-search")
    query.add_argument("title")
    for name in ("material-prepare", "setlist-propose", "setlist-confirm",
                 "artifact-register", "artifact-review", "artifact-finalize", "artifact-retire", "cleanup",
                 "response-options", "response-preview", "response-confirm", "response-resources"):
        sub = commands.add_parser(name)
        sub.add_argument("--input", default="-")
    export_set = commands.add_parser("setlist-export")
    export_set.add_argument("--output", type=Path, required=True)
    export_set.add_argument("--draft", action="store_true")
    export_set.add_argument("--reuse-existing", action="store_true",
                            help="Reuse an exact unchanged export instead of creating a redundant copy")
    for name in ("requirements", "propose", "feedback", "confirm", "review-resources",
                 "spoken-requirements", "spoken-propose", "spoken-feedback", "spoken-confirm"):
        sub = commands.add_parser(name)
        sub.add_argument("--input", default="-", help="JSON file path or - for stdin")
    for name in ("key-requirements", "key-propose", "key-feedback", "key-confirm", "key-preview"):
        sub = commands.add_parser(name)
        sub.add_argument("--input", default="-", help="JSON file path or - for stdin")
    commands.add_parser("key-options")
    commands.add_parser("resources")
    transfer = commands.add_parser("download")
    transfer.add_argument("--output", type=Path)
    export = commands.add_parser("spoken-export")
    export.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.command == "new":
        state = engine.new_state()
        if args.test_run:
            state["test_run"] = True
        directory = args.runs_dir.resolve() / state["session_id"]
        directory.mkdir(parents=True, mode=0o700)
        path = directory / "state.json"
        persist(path, state)
        emit({"state_path": str(path), **state})
        return
    if args.command == "doctor":
        try:
            result = subprocess.run(
                ["pco", "me"], capture_output=True, text=True, timeout=90, check=True,
            )
            engine.require(bool(result.stdout.strip()), "pco me returned no identity.")
        except (OSError, subprocess.SubprocessError):
            raise WorkflowError("auth_error", "pco me failed; check local authentication/Keychain access.") from None
        emit({"pco_me": "success", "credentials_logged": False})
        return
    api = PCO()
    if args.command == "catalog":
        emit({"songs": api.catalog(args.query), "scope": "non-hidden song catalog; arrangements not scanned"})
        return
    engine.require(args.state is not None, "--state is required for this command.")
    path = args.state.resolve()
    with locked(path) as state:
        command = args.command
        if command == "show":
            emit(state)
            return
        if command == "status":
            emit(status.summary(state))
            return
        if command == "spoken-context":
            emit(setlist.spoken_context(state))
            return
        if command in ("requirements", "propose", "feedback", "confirm"):
            getattr(engine, command)(state, read_input(args.input))
            result = state
        elif command == "rag-search":
            result = setlist.search(state, args.title, args.rag_db)
        elif command == "material-prepare":
            result = setlist.prepare(state, read_input(args.input), path.parent, args.rag_db)
        elif command in ("setlist-propose", "setlist-confirm"):
            result = getattr(setlist, command[len("setlist-"):])(state, read_input(args.input))
        elif command == "setlist-export":
            result = setlist.export(state, args.output, args.draft, args.reuse_existing)
        elif command == "artifact-register":
            data = read_input(args.input)
            engine.require(data.get("kind") == "temporary", "Only generated temporary outputs may be registered manually.")
            owner = str(Path(data.get("owner", "")).resolve())
            engine.require(any(x["kind"] == "final" and x["path"] == owner
                               for x in state.get("artifacts", [])), "Choose this session's exported owner DOCX.")
            candidate = Path(data.get("path", ""))
            engine.require(not candidate.is_symlink() and candidate.resolve().is_relative_to(path.parent.resolve())
                           and str(candidate.resolve()) not in artifacts.protected_paths(state),
                           "Temporary registration must stay inside this session and exclude referenced resources.")
            engine.require(candidate.resolve().is_relative_to(path.parent / "render-cache")
                           and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf"},
                           "Only generated previews in the dedicated session render-cache may be cleaned.")
            result = artifacts.register(state, candidate, "temporary", owner)
        elif command == "artifact-review":
            result = artifacts.review(state, read_input(args.input))
        elif command == "artifact-finalize":
            result = artifacts.finalize(state, read_input(args.input))
        elif command == "artifact-retire":
            result = artifacts.retire(state, read_input(args.input))
        elif command == "cleanup":
            result = artifacts.cleanup(state, read_input(args.input))
        elif command.startswith("response-"):
            action = command[len("response-"):]
            data = read_input(args.input)
            if action == "options":
                result = supplement.options(state, data, api)
            else:
                directory = path.parent / ("response-previews" if action == "preview" else "response-downloads")
                directory.mkdir(parents=True, exist_ok=True)
                result = getattr(supplement, action)(state, data, api, directory)
        elif command.startswith("spoken-") and command != "spoken-export":
            result = getattr(spoken, command[len("spoken-"):])(state, read_input(args.input))
        elif command == "spoken-export":
            content = spoken.render(state)
            output = downloads.save_original(
                args.output_dir or path.parent, "spoken-script.md", content.encode("utf-8")
            )
            record = {"path": str(output), "revision": state["spoken"]["revision"],
                      "draft_digest": state["spoken"]["confirmation"]["draft_digest"]}
            if record not in state["spoken"]["exports"]:
                state["spoken"]["exports"].append(record)
            result = record
        elif command == "key-options":
            result = keys.refresh(state, api)
        elif command == "key-preview":
            result = keys.preview(
                state, read_input(args.input), api,
                path.parent / "previews" / f'key-r{(state.get("keys") or {}).get("revision", 0)}',
                lambda: persist(path, state),
            )
        elif command.startswith("key-"):
            result = getattr(keys, command[len("key-"):])(state, read_input(args.input))
        elif command == "inspect":
            rows, errors = [], []
            for sid in args.song_ids:
                try:
                    rows.extend(api.inspect(sid))
                except WorkflowError as error:
                    errors.append({"song_id": sid, "status": error.code, "message": str(error)})
                    if error.code == "auth_error":
                        break
            engine.add_candidates(state, rows)
            result = {"arrangements": rows, "errors": errors}
        elif command == "resources":
            plan = engine.confirmed_plan(state)
            keyplan = keys.confirmed(state)
            key_by_song = {s["song_id"]: s["key_id"] for s in keyplan["songs"]}
            result = {}
            for song in plan["songs"]:
                try:
                    reused = setlist.reused(state, song["song_id"])
                    if reused:
                        result[song["song_id"]] = reused
                        continue
                    result[song["song_id"]] = downloads.public_inventory(downloads.inventory(
                        api, dict(song, selected_key_id=key_by_song[song["song_id"]])
                    ))
                except WorkflowError as error:
                    result[song["song_id"]] = {"status": error.code, "message": str(error)}
                    if error.code == "auth_error":
                        break
        elif command == "review-resources":
            result = downloads.review_resources(state, read_input(args.input), api)
        elif command == "download":
            result = downloads.download(
                state, api, args.output or path.parent / "downloads", lambda: persist(path, state)
            )
        persist(path, state)
        emit(result)
        if command == "artifact-finalize" and not result["complete"]:
            sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except WorkflowError as error:
        emit({"error": error.code, "message": str(error)})
        sys.exit(2)
    except (OSError, ValueError, KeyError, TypeError):
        # Raw exception messages can contain response bodies, credentials or signed URLs.
        emit({"error": "operation_failed", "message": "Invalid local state/input or unexpected response; nothing is confirmed by this error."})
        sys.exit(2)
