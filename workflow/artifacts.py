"""Hash-bound output acceptance and narrow, session-owned temporary cleanup."""
import copy
import hashlib
import importlib.util
from pathlib import Path

import engine as e

PAGE_CHECKS = {"chinese", "clipping", "overlap", "pagination"}
SONG_CHECKS = {"heading_baseline", "first_lyric_baseline", "table_row_heights",
               "table_content_width", "instrument_notation", "fonts", "font_sizes",
               "section_colors", "paragraph_spacing", "lyric_spacing", "table_style",
               "table_dimensions", "single_song_per_page", "lyrics_pagination",
               "left_edges", "table_lyric_gap", "table_width_balance", "section_spacing"}
RESPONSE_CHECKS = {"fonts", "font_sizes", "section_colors", "paragraph_spacing",
                   "table_style", "table_dimensions", "instrument_notation", "separate_page", "left_edges"}
FINALIZATION_VERSION = 1


def visual_evidence(payload):
    visual = payload.get("visual")
    e.require(isinstance(visual, dict) and visual.get("app") in {"WPS", "Microsoft Word"}
              and type(visual.get("page_count")) is int and visual["page_count"] > 0,
              "Record native Word/WPS application and actual page count.")
    pages = visual.get("pages")
    e.require(isinstance(pages, list) and len(pages) == visual["page_count"],
              "Record visual checks separately for every page.")
    seen = set()
    block_pages = {f"main:{i}": [] for i in (1, 2, 3)}
    response_pages = []
    for page in pages:
        e.require(isinstance(page, dict) and type(page.get("page")) is int
                  and page["page"] not in seen and e.text(page.get("note"))
                  and isinstance(page.get("checks"), list) and PAGE_CHECKS <= set(page["checks"]),
                  "Each unique page needs Chinese, clipping, overlap, pagination checks and an observation.")
        seen.add(page["page"])
        blocks = page.get("blocks")
        e.require(isinstance(blocks, list) and blocks
                  and all(isinstance(block, str) for block in blocks),
                  "Identify the actual song/appendix blocks on every rendered page.")
        if len(blocks) == 1 and blocks[0] in block_pages:
            block_pages[blocks[0]].append(page["page"])
        elif set(blocks) == {"response", "ending"} and len(blocks) == 2:
            response_pages.append(page["page"])
        else:
            e.require(blocks == ["spoken"],
                      "Never mix main songs, response/ending or spoken appendix on one page.")
    e.require(seen == set(range(1, visual["page_count"] + 1)), "Do not omit or invent page numbers.")
    e.require(len(response_pages) == 1, "Response and ending must share exactly one separate page.")
    response = visual.get("response_ending")
    e.require(isinstance(response, dict) and type(response.get("page")) is int
              and response["page"] == response_pages[0] and e.text(response.get("note"))
              and isinstance(response.get("checks"), list) and RESPONSE_CHECKS <= set(response["checks"]),
              "Record fonts, colors, spacing and table checks for the separate response/ending page.")
    songs = visual.get("songs")
    e.require(isinstance(songs, list) and len(songs) == 3, "Check each of the three main songs.")
    positions = set()
    for song in songs:
        e.require(isinstance(song, dict) and type(song.get("position")) is int
                  and song["position"] not in positions and e.text(song.get("note"))
                  and isinstance(song.get("checks"), list) and SONG_CHECKS <= set(song["checks"]),
                  "Check both baselines, row heights, content width and notation for each song.")
        positions.add(song["position"])
        pages = sorted(block_pages.get(f'main:{song["position"]}', []))
        e.require(isinstance(song.get("pages"), list) and isinstance(song.get("lyrics_pages"), list)
                  and all(type(n) is int for n in song["pages"] + song["lyrics_pages"])
                  and pages and song["pages"] == pages and song["lyrics_pages"] == pages,
                  "Record every actual song/lyric page; do not leave an orphan title or hide a continuation.")
        e.require(pages == list(range(pages[0], pages[-1] + 1)), "A song's pages must be consecutive.")
        if len(pages) > 1:
            exception = song.get("pagination_exception")
            e.require(isinstance(exception, dict) and e.text(exception.get("reason"))
                      and isinstance(exception.get("adjustments"), list) and exception["adjustments"]
                      and all(e.text(item) for item in exception["adjustments"]),
                      "Prefer a complete song on one page; explain unavoidable splits and attempted layout fixes.")
    e.require(positions == {1, 2, 3}, "Visual song checks must cover positions 1, 2 and 3.")
    ordered = [sorted(block_pages[f"main:{i}"]) for i in (1, 2, 3)]
    e.require(max(ordered[0]) < min(ordered[1]) and max(ordered[1]) < min(ordered[2])
              and max(ordered[2]) < response_pages[0],
              "Rendered pages must follow main 1, main 2, main 3, then response/ending.")
    spoken_pages = [p["page"] for p in visual["pages"] if p["blocks"] == ["spoken"]]
    e.require(not spoken_pages or min(spoken_pages) > response_pages[0],
              "Spoken appendix must follow the separate response/ending page.")
    return visual


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def accepted_review(state, owner):
    row = next((row for row in state.get("artifacts", []) if row["path"] == owner), {})
    review = state.get("artifact_reviews", {}).get(owner, {})
    return review if review.get("review_version", 1) >= row.get("required_review_version", 1) else {}


def remaining(state, owner):
    return [
        {"path": row["path"], "action": ("cleanup_preflight"
         if row["kind"] in {"temporary", "obsolete"} else "check_open_or_modified_then_retire")}
        for row in state.get("artifacts", [])
        if not row.get("deleted_at") and row["path"] != owner
        and row["kind"] in {"final", "temporary", "obsolete"}
    ]


def finalization_record(state, owner):
    review_record = accepted_review(state, owner)
    return next((row for row in reversed(state.get("cleanup_history", []))
        if review_record and row.get("kind") == "finalization" and row.get("complete") is True
        and row.get("path") == owner and row.get("sha256") == review_record.get("sha256")
        and row.get("version", 0) >= FINALIZATION_VERSION), {})


def finalized(state, owner):
    return bool(finalization_record(state, owner) and not remaining(state, owner))


def sources_valid(state, owner):
    rows = [r for r in state.get("artifacts", [])
            if r["kind"] == "sidecar" and r.get("owner") == owner]
    return bool(rows) and all(Path(r["path"]).is_file() and not Path(r["path"]).is_symlink()
                              and sha(r["path"]) == r["sha256"] for r in rows)


def register(state, path, kind, owner=None):
    path = Path(path)
    e.require(kind in {"final", "sidecar", "temporary"} and path.is_file()
              and not path.is_symlink(), "Register an existing regular artifact.")
    path = path.resolve()
    entry = {"path": str(path), "sha256": sha(path), "kind": kind,
             "owner": str(Path(owner).resolve()) if owner else None, "at": e.now()}
    rows = state.setdefault("artifacts", [])
    e.require(not any(x["path"] == str(path) and
                      any(x[k] != entry[k] for k in ("sha256", "kind", "owner")) for x in rows),
              "Artifact already registered; never replace ownership or its saved hash.")
    if not any(x["path"] == str(path) for x in rows):
        rows.append(entry)
    return entry


def review(state, payload):
    e.safe_input(payload)
    path = str(Path(payload.get("path", "")).resolve())
    row = next((x for x in state.get("artifacts", [])
                if x["path"] == path and x["kind"] == "final"), None)
    e.require(row and Path(path).is_file() and not Path(path).is_symlink()
              and sha(path) == row["sha256"] == payload.get("sha256")
              and sources_valid(state, path),
              "Review must identify an unchanged exported DOCX.")
    e.require(payload.get("confirmed") is True and e.text(payload.get("user_message"))
              and {"layout", "colors", "content", "pagination"} <= set(payload.get("checks", [])),
              "Record actual visual acceptance of layout, colors, content and pagination.")
    visual = visual_evidence(payload)
    spec = importlib.util.spec_from_file_location(
        "artifact_template_renderer", Path(__file__).resolve().parents[1] / "rag/template_renderer.py")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    structural = renderer.inspect_docx(path)
    result = {"path": path, "sha256": row["sha256"], "at": e.now(),
              "user_message": payload["user_message"], "checks": payload["checks"],
              "visual": visual, "structural": structural, "review_version": renderer.LAYOUT_POLICY_VERSION}
    state.setdefault("artifact_reviews", {})[path] = result
    return result


def protected_paths(state):
    """All non-artifact path references stay protected, including old workflow history."""
    result = set()
    def visit(value):
        if isinstance(value, dict):
            if value.get("retired_for_cleanup"):
                return
            for k, v in value.items():
                if k not in {"artifacts", "artifact_reviews", "cleanup_history"}:
                    visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)
        elif isinstance(value, str) and value.startswith("/"):
            result.add(str(Path(value).resolve()))
    visit(state)
    for row in state.get("artifacts", []):
        if row["kind"] not in {"temporary", "obsolete"}:
            result.add(row["path"])
    return result


def retire(state, payload):
    """Explicit replacement of an owned old export; retain audit entries, never dangling active refs."""
    e.safe_input(payload)
    target = str(Path(payload.get("path", "")).resolve())
    owner = str(Path(payload.get("replacement", "")).resolve())
    review_record = accepted_review(state, owner)
    e.require(target != owner and review_record and Path(owner).is_file()
              and sha(owner) == review_record["sha256"] and sources_valid(state, owner)
              and payload.get("confirmed") is True
              and e.text(payload.get("user_message")),
              "Explicitly identify the visually accepted replacement of an obsolete export.")
    rows = state.get("artifacts", [])
    target_row = next((r for r in rows if r["path"] == target and r["kind"] == "final"), None)
    e.require(target_row and payload.get("sha256") == target_row["sha256"]
              and Path(target).is_file() and not Path(target).is_symlink()
              and sha(target) == target_row["sha256"], "Old export is not owned or has user edits.")
    sidecars = [r for r in rows if r["kind"] == "sidecar" and r.get("owner") == target]
    e.require(sidecars and all(Path(r["path"]).is_file() and not Path(r["path"]).is_symlink()
                              and sha(r["path"]) == r["sha256"] for r in sidecars),
              "Old sources sidecar is missing or changed; do not delete the pair.")
    exports = []
    def find(value):
        if isinstance(value, dict):
            if value.get("path") == target and "sources" in value and "status" in value:
                exports.append(value)
            for v in value.values():
                find(v)
        elif isinstance(value, list):
            for v in value:
                find(v)
    find(state.get("setlist", {}))
    e.require(exports, "Only actual session exports can be retired.")
    for exported in exports:
        exported.update(retired_for_cleanup=True, superseded_by=owner,
                        retirement_message=payload["user_message"])
    for row in [target_row, *sidecars]:
        row.update(kind="obsolete", owner=owner, retired_at=e.now())
    return {"retired": [r["path"] for r in [target_row, *sidecars]], "replacement": owner}


def cleanup(state, payload):
    e.safe_input(payload)
    e.require(type(payload.get("apply", False)) is bool, "apply must be boolean.")
    candidates, skipped, deleted = [], [], []
    protected = protected_paths(state)
    for row in state.get("artifacts", []):
        if row["kind"] not in {"temporary", "obsolete"} or row.get("deleted_at"):
            continue
        path = Path(row["path"])
        owner = row.get("owner")
        accepted = accepted_review(state, owner)
        if not accepted:
            if payload.get("apply"):
                e.require(False, "Visual acceptance is required before deleting temporary outputs.")
            skipped.append({"path": str(path), "reason": "awaiting_visual_acceptance"})
            continue
        # Reject symlink parents as well as leaf links; never follow a redirected directory.
        redirected = any(p.is_symlink() for p in (path, *path.parents))
        valid_owner = (Path(owner).is_file() and not Path(owner).is_symlink()
                       and sha(owner) == accepted["sha256"] and sources_valid(state, owner))
        if (str(path) in protected or redirected or not path.is_file()
                or sha(path) != row["sha256"] or not valid_owner):
            skipped.append({"path": str(path), "reason": "protected_missing_or_changed"})
        else:
            candidates.append(row)
    # Complete preflight precedes the first deletion.
    if payload.get("apply"):
        for row in candidates:
            path = Path(row["path"])
            if not path.is_symlink() and path.is_file() and sha(path) == row["sha256"]:
                path.unlink()
                row["deleted_at"] = e.now()
                deleted.append(str(path))
    result = {"dry_run": not payload.get("apply", False),
              "candidates": [x["path"] for x in candidates], "deleted": deleted, "skipped": skipped}
    if payload.get("apply"):
        state.setdefault("cleanup_history", []).append({**result, "at": e.now()})
    return result


def finalize(state, payload):
    """Review and remove superseded exports in one preflighted, session-owned operation."""
    import setlist

    e.safe_input(payload)
    part = setlist.current(state)
    exports = [row for row in part["exports"] if not row.get("retired_for_cleanup")]
    owner = str(Path(payload.get("path", "")).resolve())
    e.require(exports and exports[-1]["path"] == owner and part.get("stage") == "confirmed"
              and part.get("confirmation")
              and part["confirmation"].get("selection_digest") == e.digest(part["selection"])
              and (exports[-1]["status"] == "exported"
                   or exports[-1]["status"] == "test_artifact"
                   and exports[-1].get("required_cleanup_version")),
              "Finalize only the latest confirmed formal export, never an old or draft document.")
    inspection = payload.get("cleanup")
    e.require(isinstance(inspection, dict) and e.text(inspection.get("user_message"))
              and isinstance(inspection.get("checked_files"), list),
              "Record actual closed-file inspection before finalization, including an empty checked_files list.")

    # All ownership, visual and deletion checks run on a copy before the first mutation/unlink.
    working = copy.deepcopy(state)
    e.require(type(payload.get("use_existing_review", False)) is bool,
              "use_existing_review must be boolean.")
    if payload.get("use_existing_review"):
        review_record = accepted_review(working, owner)
        e.require(review_record and review_record.get("sha256") == payload.get("sha256")
                  == exports[-1]["sha256"] and Path(owner).is_file()
                  and not Path(owner).is_symlink() and sha(owner) == payload["sha256"]
                  and sources_valid(working, owner),
                  "Reuse only this unchanged formal export's valid hash-bound native review.")
    else:
        review_record = review(working, payload)
    old = [row for row in working.get("artifacts", [])
           if row["kind"] == "final" and row["path"] != owner and not row.get("deleted_at")]
    for row in old:
        retire(working, {"path": row["path"], "sha256": row["sha256"], "replacement": owner,
                         "confirmed": True, "user_message": inspection["user_message"]})
    # Previously retired pairs/previews may point at an older replacement.
    for row in working.get("artifacts", []):
        if row["kind"] in {"temporary", "obsolete"} and not row.get("deleted_at"):
            row["owner"] = owner
    plan = cleanup(working, {})
    e.require(not plan["skipped"], "Cleanup preflight blocked; preserve all files and resolve protected/missing/changed items.")
    expected = set(plan["candidates"])
    checks = inspection["checked_files"]
    e.require(all(isinstance(item, dict) and isinstance(item.get("path"), str)
                  and item.get("closed") is True for item in checks),
              "Every deletion candidate must be explicitly observed closed; do not close or discard user edits.")
    e.require(len(checks) == len(expected) and {item["path"] for item in checks} == expected,
              "Closed-file inspection must exactly cover all obsolete DOCX/sources and registered previews.")
    registered = {row["path"]: row for row in working["artifacts"]}
    e.require(all(item.get("sha256") == registered[item["path"]]["sha256"] for item in checks),
              "Closed-file inspection must bind each candidate's registered hash.")
    # Recheck the entire batch after inspection, before cleanup performs any unlink.
    verified = cleanup(working, {})
    e.require(not verified["skipped"] and set(verified["candidates"]) == expected,
              "Cleanup candidates changed during preflight; nothing may be removed.")
    try:
        applied = cleanup(working, {"apply": True})
        complete = not applied["skipped"] and not remaining(working, owner)
        error = None
    except OSError:
        # Preserve already-completed deletions in state so a retry cannot lose their audit trail.
        applied = {"deleted": [row["path"] for row in working["artifacts"]
                               if row["path"] in expected and row.get("deleted_at")],
                   "skipped": []}
        complete, error = False, "cleanup_io_error"
    result = {"kind": "finalization", "version": FINALIZATION_VERSION,
              "path": owner, "sha256": review_record["sha256"], "complete": complete,
              "at": e.now(), "inspection": inspection, "preflight": plan,
              "deleted": applied["deleted"], "error": error}
    working.setdefault("cleanup_history", []).append(result)
    state.clear()
    state.update(working)
    return result
