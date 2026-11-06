#!/usr/bin/env python3
"""PCO-only setlist assembly, without selection, audio or RAG confirmation gates."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "workflow"))
import downloads
import engine as e
from pco_api import PCO, WorkflowError, identifier
import delivery
from file_lock import exclusive


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def read_input(path):
    if path == "-":
        value = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    e.require(isinstance(value, dict), "Input must be a JSON object.")
    e.safe_input(value)
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


def new_state():
    return {"mode": "mini", "schema_version": 1, "session_id": uuid.uuid4().hex,
            "created_at": e.now(), "revision": 0, "selection": None,
            "candidates": {}, "results": {}, "resource_reviews": {},
            "materials": {}, "exports": [], "artifacts": []}


def identity(song):
    return (song["song_id"] + ":" + song["arrangement_id"] if song.get("song_id")
            else "not_found:" + song["title"])


def songs(state):
    selection = state.get("selection")
    e.require(selection, "Select three ordered main songs first.")
    result = {}
    for song in [*selection["main"], selection["response"], selection["ending"]]:
        if song:
            result.setdefault(identity(song), song)
    return list(result.values())


def normalized(title):
    return "".join(unicodedata.normalize("NFKC", title).casefold().split())


def resource_binding(song):
    return e.digest({k: song.get(k) for k in (
        "song_id", "arrangement_id", "title", "bpm", "meter", "source_updated_at", "lyrics_sha256")})


def inspect(state, api, ids):
    rows = []
    for sid in ids:
        found = api.inspect(identifier(sid))
        for row in found:
            saved = {k: v for k, v in row.items() if k != "lyrics"}
            saved["lyrics_sha256"] = hashlib.sha256(row.get("lyrics", "").encode()).hexdigest()
            saved["source_updated_at"] = row.get("updated_at")
            state["candidates"][identity(saved)] = saved
        rows.extend(found)
    return {"arrangements": rows}


def scripture(value):
    if value is None:
        return None
    e.require(isinstance(value, dict) and set(value) <= {
        "reference", "translation", "quote", "source", "verification", "user_message"},
        "Invalid scripture record.")
    e.require(all(e.text(value.get(k)) for k in
                  ("reference", "translation", "quote", "source", "user_message")),
              "Scripture needs the user's instruction and reference, translation, quote, source.")
    e.require(value.get("verification") in {"source_checked", "unverified"},
              "Record actual scripture verification, or explicitly mark unverified.")
    return copy.deepcopy(value)


def select(state, payload, api):
    e.require(set(payload) <= {"main", "response", "ending", "spoken"},
              "Only main, response, ending and user-provided spoken content are supported.")
    main = payload.get("main")
    e.require(isinstance(main, list) and len(main) == 3, "Supply exactly three ordered main songs.")

    def resolve(spec):
        e.require(isinstance(spec, dict) and set(spec) <= {
            "title", "song_id", "arrangement_id", "scripture"}, "Invalid song selection.")
        if "song_id" in spec:
            sid, aid = identifier(spec["song_id"]), identifier(spec.get("arrangement_id"))
            row = state["candidates"].get(sid + ":" + aid)
            e.require(row, "Inspect the real song/arrangement before selecting its IDs.")
            if "title" in spec:
                e.require(spec["title"] == row["title"], "Title does not match the inspected song.")
        else:
            e.require(e.text(spec.get("title")) and "arrangement_id" not in spec,
                      "Supply a title or inspected song/arrangement IDs.")
            matches = api.catalog(spec["title"])
            exact = [s for s in matches if normalized(s["title"]) == normalized(spec["title"])]
            if not matches:
                return {"title": spec["title"], "status": "not_found",
                        "scripture": scripture(spec.get("scripture"))}
            if len(exact) != 1:
                raise WorkflowError("needs_review", "Title is ambiguous; use catalog/inspect and select explicit IDs.")
            found = inspect(state, api, [exact[0]["song_id"]])["arrangements"]
            e.require(found, "Song has no active arrangements; inspect the library before continuing.")
            if len(found) != 1:
                raise WorkflowError("needs_review", "Multiple active arrangements; inspect and select explicit IDs.")
            row = state["candidates"][identity(found[0])]
        return {**copy.deepcopy(row), "scripture": scripture(spec.get("scripture"))}

    resolved = [resolve(spec) for spec in main]
    e.require(len({s.get("song_id") or normalized(s["title"]) for s in resolved}) == 3,
              "The three main songs must be distinct.")
    selection = {"main": resolved}
    for role in ("response", "ending"):
        spec = payload.get(role)
        if isinstance(spec, dict) and set(spec) == {"main_position"}:
            position = spec["main_position"]
            e.require(type(position) is int and 1 <= position <= 3, "main_position must be 1, 2 or 3.")
            selection[role] = copy.deepcopy(resolved[position - 1])
        else:
            selection[role] = resolve(spec) if spec is not None else None
    spoken = payload.get("spoken", [])
    e.require(isinstance(spoken, list), "spoken must be a list of user-provided texts.")
    for item in spoken:
        e.require(isinstance(item, dict) and set(item) == {"title", "text", "user_message"}
                  and all(e.text(v) for v in item.values()),
                  "Spoken content must include title, text and actual user instruction.")
    selection["spoken"] = copy.deepcopy(spoken)
    state["selection"] = selection
    state["revision"] += 1
    return {"selection": selection, "revision": state["revision"]}


def inventory(state, api):
    result = {}
    for song in songs(state):
        key = identity(song)
        if not song.get("song_id"):
            result[key] = {"status": "not_found", "title": song["title"]}
            continue
        try:
            result[key] = {"sources": downloads.public_inventory(downloads.inventory(api, song))}
        except WorkflowError as error:
            result[key] = {"status": error.code, "message": str(error)}
            if error.code == "auth_error":
                break
    return result


def review_resources(state, payload, api):
    e.require(set(payload) <= {"song_id", "arrangement_id", "classifications",
                              "selected_source_id", "user_message"}, "Unknown resource review fields.")
    key = identifier(payload.get("song_id")) + ":" + identifier(payload.get("arrangement_id"))
    song = next((s for s in songs(state) if identity(s) == key), None)
    e.require(song, "Only review selected songs.")
    sources = {s["id"]: s for s in downloads.inventory(api, song)}
    reviews = {}
    for row in payload.get("classifications", []):
        source = sources.get(row.get("source_id"))
        e.require(source and row.get("kind") in {downloads.CHINESE_LYRICS, downloads.SCORE, "other"}
                  and e.text(row.get("evidence")), "Classification needs a real source and evidence.")
        e.require(source["kind"] != downloads.CHINESE_LYRICS or row["kind"] == downloads.CHINESE_LYRICS,
                  "Do not relabel Chinese lyrics to bypass priority.")
        reviews[source["id"]] = {"kind": row["kind"], "evidence": row["evidence"],
                                 "fingerprint": downloads.source_fingerprint(source)}
    choice = payload.get("selected_source_id")
    e.require(choice is None or choice in sources and e.text(payload.get("user_message")),
              "An ambiguous source choice needs the user's actual instruction.")
    state["resource_reviews"][key] = {
        "classifications": reviews, "selected_source_id": choice,
        "choice_fingerprint": downloads.source_fingerprint(sources[choice]) if choice else None,
        "user_message": payload.get("user_message")}
    # A new explicit choice must not silently reuse the previously downloaded version.
    state["results"].pop(key, None)
    state["materials"].pop(key, None)
    return {"review_saved": True, "identity": key}


def download(state, api, directory, save=lambda: None):
    selected = songs(state)
    for index, song in enumerate(selected):
        key = identity(song)
        old = state["results"].get(key, {})
        path = Path(old["path"]) if old.get("path") else None
        try:
            if not song.get("song_id"):
                result = {"status": "not_found", "message": "No matching PCO song."}
            else:
                sources = downloads.inventory(api, song)
                source = downloads.choose(sources, state["resource_reviews"].get(key, {}))
                if source is None:
                    result = {"status": "missing", "message": "No Chinese lyrics or numbered score in checked active sources."}
                elif (path and path.is_file() and not path.is_symlink()
                      and sha(path) == old.get("sha256")
                      and old.get("source_fingerprint") == downloads.source_fingerprint(source)
                      and old.get("song_binding") == resource_binding(song)):
                    result = old
                else:
                    content = source["text"].encode("utf-8") if "text" in source else api.file(source["attachment_id"])
                    downloads.validate_file(content, source["filename"], source["kind"])
                    path = downloads.save_original(directory / key.replace(":", "-"), source["filename"], content)
                    result = {"status": "downloaded", "kind": source["kind"], "path": str(path),
                              "sha256": sha(path), "source": downloads.public_inventory([source])[0],
                              "source_fingerprint": downloads.source_fingerprint(source),
                              "score_key_status": "not_selected" if source["kind"] == downloads.SCORE else None}
        except WorkflowError as error:
            result = {"status": error.code, "message": str(error)}
        except OSError:
            result = {"status": "file_error", "message": "Local file operation failed."}
        if result != old:
            state["materials"].pop(key, None)
        state["results"][key] = {"title": song["title"], **result, "song_binding": resource_binding(song)}
        save()
        if result["status"] == "auth_error":
            for pending in selected[index + 1:]:
                pending_key = identity(pending)
                # Keep successful evidence; only unattempted songs get a blocked status.
                if pending_key not in state["results"]:
                    state["results"][pending_key] = {
                        "title": pending["title"], "status": "not_checked",
                        "message": "PCO authentication failed earlier; this song has not been checked.",
                        "song_binding": resource_binding(pending)}
            save()
            break
    return state["results"]


def original_text(path):
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        return "\n".join("".join(t.text or "" for t in p.iter()
                                if t.tag.endswith("}t")) for p in root.iter()
                         if p.tag.endswith("}p"))
    return None


def label(value):
    e.require(e.text(value), "Every section needs a label.")
    compact = re.sub(r"[\s_-]+", "", value.upper())
    for name, short in (("PRECHORUS", "PC"), ("CHORUS", "C"), ("VERSE", "V"), ("BRIDGE", "B")):
        if re.fullmatch(name + r"\d*", compact):
            compact = short + compact[len(name):]
            break
    compact = "IN" if compact in {"INT", "INS"} else compact
    e.require(re.fullmatch(r"(?:V|C|PC|B|LL|TAG)\d*|IN|END|OUT|OUTRO", compact),
              "Use recognized lyric or instrumental labels.")
    return compact


def prepare(state, payload):
    e.require(set(payload) <= {"song_id", "arrangement_id", "source_sha256", "version_basis",
                              "lyric_sections", "route", "structure_kind", "structure_basis",
                              "analysis", "issues"}, "Unknown material fields (key/BPM are never accepted).")
    key = identifier(payload.get("song_id")) + ":" + identifier(payload.get("arrangement_id"))
    e.require(any(identity(s) == key for s in songs(state)), "Only prepare selected songs.")
    result = state["results"].get(key, {})
    e.require(result.get("status") == "downloaded", "Download the original PCO document first.")
    path = Path(result["path"])
    e.require(path.is_file() and not path.is_symlink()
              and sha(path) == result["sha256"] == payload.get("source_sha256"),
              "Material must bind an unchanged downloaded original.")
    e.require(e.text(payload.get("version_basis")), "Explain the version/arrangement identity.")
    sections = payload.get("lyric_sections", [])
    route = payload.get("route", [])
    issues = payload.get("issues", [])
    e.require(isinstance(sections, list) and isinstance(route, list)
              and isinstance(issues, list) and all(e.text(x) for x in issues), "Invalid material lists.")
    labels = set()
    for section in sections:
        e.require(isinstance(section, dict) and set(section) == {"label", "lines"},
                  "Each lyric section has label and original lines.")
        section_label = label(section["label"])
        e.require(section_label not in labels and re.fullmatch(r"(?:V|C|PC|B|LL|TAG)\d*", section_label),
                  "Lyric section labels must be distinct and cannot be instrumental.")
        e.require(isinstance(section["lines"], list) and section["lines"]
                  and all(e.text(line) for line in section["lines"]), "Supply nonempty original lyric lines.")
        labels.add(section_label)
    text = original_text(path)
    analysis = payload.get("analysis")
    if text is not None:
        source_lines = [line.strip() for line in text.splitlines() if line.strip()]
        e.require(all(line.strip() in source_lines for s in sections for line in s["lines"]),
                  "Each lyric line must occur in the original text; no rewriting.")
        # LL/Tag are independent only when explicitly labelled in the source.
        for section_label in labels:
            if section_label.startswith(("LL", "TAG")):
                e.require(any(re.fullmatch(r"[\[\(【]?\s*" + re.escape(section_label)
                                          + r"\s*[\]\)】:]?", line, re.I) for line in source_lines),
                          "LL/Tag need explicit independent source labels.")
    else:
        e.require(isinstance(analysis, dict) and analysis.get("verified") is True
                  and e.text(analysis.get("method")) and e.text(analysis.get("note")),
                  "Score/PDF/image reading requires actual verified analysis.")
        evidence = analysis.get("sections", {})
        e.require(isinstance(evidence, dict) and set(evidence) == labels
                  and all(e.text(v) for v in evidence.values()),
                  "Record page/region evidence for every transcribed lyric section.")
    kind = payload.get("structure_kind", "unknown")
    e.require(kind in {"source_explicit", "proposal", "user_provided", "unknown"},
              "Structure kind must distinguish source, proposal, user instruction and unknown.")
    e.require((not route and kind == "unknown") or
              (route and kind != "unknown" and e.text(payload.get("structure_basis"))),
              "Routes need their actual basis; an unknown route stays empty.")
    for item in route:
        e.require(isinstance(item, dict) and set(item) <= {"label", "current_cue", "cue_basis"}
                  and label(item.get("label")) in labels | {"IN", "END", "OUT", "OUTRO"},
                  "Route references an absent lyric section.")
        if item.get("current_cue"):
            e.require(e.text(item["current_cue"]) and e.text(item.get("cue_basis")),
                      "Bars, assignments and dynamics require explicit source/user evidence.")
    if text is None and route and kind == "source_explicit":
        evidence = analysis.get("route", {})
        e.require(isinstance(evidence, dict) and set(evidence) == {str(i) for i in range(len(route))}
                  and all(e.text(v) for v in evidence.values()),
                  "Record page/region evidence for every source-derived route occurrence.")
    material = {**copy.deepcopy(payload), "lyric_sections": [
        {"label": label(s["label"]), "lines": s["lines"]} for s in sections],
        "route": [dict(item, label=label(item["label"])) for item in route],
        "structure_kind": kind, "issues": issues, "source_path": str(path)}
    state["materials"][key] = material
    return {"identity": key, "material": material}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new")
    new.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    commands.add_parser("doctor")
    catalog = commands.add_parser("catalog")
    catalog.add_argument("--query")
    inspection = commands.add_parser("inspect")
    inspection.add_argument("song_ids", nargs="+")
    for name in ("show", "status", "resources", "download"):
        commands.add_parser(name)
    for name in ("select", "review-resources", "material-prepare", "artifact-register", "artifact-finalize"):
        commands.add_parser(name).add_argument("--input", default="-")
    output = commands.add_parser("export")
    output.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "new":
        state = new_state()
        folder = args.runs_dir.resolve() / state["session_id"]
        folder.mkdir(parents=True, mode=0o700)
        persist(folder / "state.json", state)
        emit({"state_path": str(folder / "state.json"), **state})
        return
    api = PCO()
    if args.command == "doctor":
        # The same API used for download; no credentials are returned.
        api.request("/services/v2/songs?per_page=1")
        emit({"pco": "success", "credentials_logged": False})
        return
    if args.command == "catalog":
        emit({"songs": api.catalog(args.query)})
        return
    e.require(args.state and args.state.is_file() and not args.state.is_symlink(),
              "Supply an existing mini --state before the subcommand.")
    path = args.state.resolve()
    with path.with_suffix(".lock").open("a+") as lock, exclusive(lock):
        state = json.loads(path.read_text(encoding="utf-8"))
        e.require(state.get("mode") == "mini" and state.get("schema_version") == 1,
                  "Not a supported mini-workflow state; never use a full workflow session here.")
        command = args.command
        if command in {"show", "status"}:
            emit(state if command == "show" else delivery.status(state))
            return
        if command == "inspect":
            result = inspect(state, api, args.song_ids)
        elif command == "select":
            result = select(state, read_input(args.input), api)
        elif command == "resources":
            emit(inventory(state, api))
            return
        elif command == "review-resources":
            result = review_resources(state, read_input(args.input), api)
        elif command == "download":
            result = download(state, api, path.parent / "downloads", lambda: persist(path, state))
        elif command == "material-prepare":
            result = prepare(state, read_input(args.input))
        elif command == "export":
            result = delivery.export(state, args.output)
        elif command == "artifact-register":
            result = delivery.register_preview(state, read_input(args.input), path.parent)
        else:
            result = delivery.finalize(state, read_input(args.input))
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
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, ElementTree.ParseError):
        emit({"error": "operation_failed",
              "message": "Invalid state/input, unavailable template or file operation failed; nothing is confirmed."})
        sys.exit(2)
