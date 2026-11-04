"""Mini-only export and native acceptance; full-workflow gates are never changed."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import artifacts
import engine as e
from pco_api import WorkflowError

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mini_template_renderer", ROOT.parent / "rag/template_renderer.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def key(song):
    return (song["song_id"] + ":" + song["arrangement_id"] if song.get("song_id")
            else "not_found:" + song["title"])


def regular(path):
    path = Path(path)
    return path.is_file() and not any(p.is_symlink() for p in (path, *path.parents))


def context(state):
    selection = state.get("selection")
    e.require(selection, "Select three ordered songs before export.")
    evidence, warnings = {}, []
    for song in [*selection["main"], selection["response"], selection["ending"]]:
        if song is None or key(song) in evidence:
            continue
        sid = key(song)
        e.require(sid in state["results"], "Run download for every selected song before exporting.")
        result = state["results"][sid]
        material = state["materials"].get(sid)
        expected = e.digest({k: song.get(k) for k in (
            "song_id", "arrangement_id", "title", "bpm", "meter", "source_updated_at", "lyrics_sha256")})
        e.require(not result.get("song_binding") or result["song_binding"] == expected,
                  "Selected PCO version changed; download/review resources again.")
        if result.get("status") == "downloaded":
            e.require(regular(result["path"]) and sha(result["path"]) == result["sha256"],
                      "Downloaded original changed or disappeared; download/review it again.")
        if material:
            e.require(result.get("status") == "downloaded"
                      and material["source_sha256"] == result["sha256"],
                      "Material no longer matches the downloaded source.")
        if result.get("status") != "downloaded":
            warnings.append({"title": song["title"], "status": result["status"],
                             "message": result.get("message", "PCO document has not been checked.")})
        if not material or not material["lyric_sections"]:
            warnings.append({"title": song["title"], "status": "lyrics_blank",
                             "message": "No verified lyric sections imported; content stays blank."})
        if not material or not material["route"]:
            warnings.append({"title": song["title"], "status": "structure_blank",
                             "message": "No sourced performance route imported."})
        elif material["structure_kind"] == "proposal":
            warnings.append({"title": song["title"], "status": "proposed_structure",
                             "message": material["structure_basis"]})
        if material:
            warnings.extend({"title": song["title"], "status": "reading_issue", "message": issue}
                            for issue in material["issues"])
        verse = song.get("scripture")
        if verse and verse["verification"] != "source_checked":
            warnings.append({"title": song["title"], "status": "scripture_unverified",
                             "message": "User-provided scripture was not independently verified."})
        evidence[sid] = {"song": song, "resource": result, "material": material}
    return evidence, warnings


def binding(state):
    evidence, warnings = context(state)
    template = ROOT.parent / "skills/pco-assemble-worship-setlist/assets/reference.docx"
    files = [ROOT / "mini.py", ROOT / "delivery.py", Path(renderer.__file__),
             ROOT.parent / "workflow/downloads.py", ROOT.parent / "workflow/pco_api.py",
             ROOT.parent / "workflow/engine.py", ROOT.parent / "workflow/artifacts.py", template]
    return e.digest({"selection": state["selection"], "evidence": evidence,
                     "implementation": {str(p.relative_to(ROOT.parent)): sha(p) for p in files}}), evidence, warnings


def unchanged(state, exported):
    return (regular(exported["path"]) and sha(exported["path"]) == exported["sha256"]
            and regular(exported["sources"])
            and sha(exported["sources"]) == exported["sources_sha256"]
            and artifacts.sources_valid(state, exported["path"]))


def export(state, output):
    fingerprint, evidence, warnings = binding(state)
    path = Path(output).absolute()
    e.require(path.suffix.lower() == ".docx", "Export output must have the .docx extension.")
    e.require(not any(p.is_symlink() for p in (path, *path.parents)), "Do not export through symlinks.")
    path = path.resolve()
    sidecar = path.with_suffix(".sources.json")
    for previous in reversed(state["exports"]):
        if (not previous.get("retired_for_cleanup") and previous["path"] == str(path)
                and previous["binding"] == fingerprint and unchanged(state, previous)):
            return {**previous, "reused": True}
    pending = [x for x in state["exports"] if not x.get("retired_for_cleanup")
               and artifacts.accepted_review(state, x["path"]) and not artifacts.finalized(state, x["path"])]
    e.require(not pending, "Finalize the reviewed export before creating another document.")
    e.require(not path.exists() and not sidecar.exists(), "Never overwrite existing DOCX or sources files.")
    doc = renderer.TemplateRenderer()
    selection = state["selection"]
    features = {}

    def content(song, full=True):
        record = evidence[key(song)]["material"] or {}
        route, sections = record.get("route", []), record.get("lyric_sections", [])
        if full:
            verse = song.get("scripture")
            value = (verse["reference"] + "（" + verse["translation"] + "）\n" + verse["quote"]) if verse else ""
            doc.write("【经文】" + value, size=12, role="scripture")
            doc.write("结构分配：", size=11, bold=True)
        if full or route:
            doc.table({"route": route or [{"label": ""} for _ in range(4)]})
        if full and sections:
            doc.lyrics({"lyric_sections": sections})
        return {"lyrics": bool(full and sections), "table": bool(full or route),
                "route_known": bool(route)}

    for position, song in enumerate(selection["main"], 1):
        doc.write(f"{position}. {song['title']}  {song.get('meter') or ''}  调=  BPM=",
                  "C00000", 16, True, new_page=position > 1)
        features[f"main:{position}"] = content(song)
    for role, name in (("response", "回应诗歌"), ("ending", "结束诗歌")):
        song = selection[role]
        doc.write(name + "：" + (song["title"] + "  调=  BPM=" if song else ""),
                  "C00000", 14, True, new_page=role == "response")
        features[role] = {"lyrics": False, "table": False}
        if song:
            matching = next((i for i, s in enumerate(selection["main"], 1) if key(s) == key(song)), None)
            if role == "response" and matching:
                doc.write(f"结构同第{matching}首", size=12)
            else:
                features[role] = content(song, full=role == "response")
    if selection["spoken"]:
        doc.write("祷告与衔接话语", "C00000", 16, True, new_page=True)
        for item in selection["spoken"]:
            doc.write(item["title"], size=12, bold=True)
            doc.write(item["text"], size=11)
    provenance = {"mode": "mini", "selection": selection, "evidence": evidence,
                  "warnings": warnings, "binding": fingerprint,
                  "key": "", "bpm": "", "rag_used": False, "audio_downloaded": False,
                  "structure_confirmed": False, "features": features}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve both names exclusively before rendering; failure never touches an existing file.
    with path.open("xb"):
        pass
    # Keep partial outputs on failure for diagnosis, without overwriting user files.
    with sidecar.open("x", encoding="utf-8") as handle:
        json.dump(provenance, handle, ensure_ascii=False, indent=2)
    doc.save(path)
    exported = {"path": str(path), "sources": str(sidecar), "sha256": sha(path),
                "sources_sha256": sha(sidecar), "binding": fingerprint, "at": e.now(),
                "status": "exported_pending_native_review", "features": features,
                "warnings": warnings, "required_review_version": 5, "required_cleanup_version": 1}
    state["exports"].append(exported)
    row = artifacts.register(state, path, "final")
    row["required_review_version"] = 5
    artifacts.register(state, sidecar, "sidecar", str(path))
    return exported


def register_preview(state, payload, directory):
    e.require(set(payload) == {"path", "owner"}, "Preview needs path and exported DOCX owner.")
    candidate = Path(payload["path"])
    owner = str(Path(payload["owner"]).resolve())
    e.require(regular(candidate) and candidate.resolve().is_relative_to(directory / "render-cache")
              and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf"}
              and any(x["path"] == owner for x in state["exports"]),
              "Only session render-cache previews with an exported owner can be registered.")
    return artifacts.register(state, candidate, "temporary", owner)


def visual_evidence(visual, features):
    e.require(isinstance(visual, dict) and visual.get("app") in {"WPS", "Microsoft Word"}
              and type(visual.get("page_count")) is int and visual["page_count"] >= 4,
              "Record actual native Word/WPS application and page count.")
    pages = visual.get("pages")
    e.require(isinstance(pages, list) and len(pages) == visual["page_count"],
              "Record every actual page.")
    block_pages = {f"main:{i}": [] for i in (1, 2, 3)}
    response, spoken, seen = [], [], set()
    for page in pages:
        e.require(isinstance(page, dict) and type(page.get("page")) is int
                  and page["page"] not in seen and e.text(page.get("note"))
                  and isinstance(page.get("checks"), list)
                  and artifacts.PAGE_CHECKS <= set(page["checks"]), "Page observation is incomplete.")
        seen.add(page["page"])
        blocks = page.get("blocks")
        if isinstance(blocks, list) and len(blocks) == 1 and blocks[0] in block_pages:
            block_pages[blocks[0]].append(page["page"])
        elif blocks == ["response", "ending"]:
            response.append(page["page"])
        else:
            e.require(blocks == ["spoken"], "Do not mix songs or supplementary blocks on a page.")
            spoken.append(page["page"])
    e.require(seen == set(range(1, visual["page_count"] + 1)) and len(response) == 1,
              "Page coverage must be consecutive, with one response/ending page.")
    song_rows = visual.get("songs")
    e.require(isinstance(song_rows, list) and len(song_rows) == 3
              and {s.get("position") for s in song_rows} == {1, 2, 3},
              "Observe each main song exactly once.")
    lyric_checks = {"heading_baseline", "first_lyric_baseline", "lyric_spacing",
                    "lyrics_pagination", "table_lyric_gap", "section_spacing"}
    for song in song_rows:
        block = f'main:{song["position"]}'
        actual = sorted(block_pages[block])
        e.require(actual and song.get("pages") == actual and e.text(song.get("note")),
                  "Song observations must identify actual pages.")
        e.require(actual == list(range(actual[0], actual[-1] + 1)), "Song pages must be consecutive.")
        omitted = set()
        if not features[block]["lyrics"]:
            omitted = lyric_checks
            reasons = song.get("not_applicable", {})
            e.require(isinstance(reasons, dict) and set(reasons) == omitted
                      and all(e.text(v) for v in reasons.values()) and song.get("lyrics_pages") == [],
                      "Blank lyrics need explicit N/A reasons and empty lyrics_pages, not fictitious lyric checks.")
        else:
            e.require(song.get("lyrics_pages") == actual and not song.get("not_applicable"),
                      "Do not waive checks or omit pages for existing lyrics.")
        e.require(isinstance(song.get("checks"), list)
                  and artifacts.SONG_CHECKS - omitted <= set(song["checks"])
                  and not omitted.intersection(song["checks"]),
                  "Song style/table/spacing observations are incomplete.")
        if len(actual) > 1:
            exception = song.get("pagination_exception", {})
            e.require(e.text(exception.get("reason")) and isinstance(exception.get("adjustments"), list)
                      and exception["adjustments"] and all(e.text(x) for x in exception["adjustments"]),
                      "Explain unavoidable lyric splits and actual layout attempts.")
    ordered = [sorted(block_pages[f"main:{i}"]) for i in (1, 2, 3)]
    e.require(max(ordered[0]) < min(ordered[1]) and max(ordered[1]) < min(ordered[2])
              and max(ordered[2]) < response[0] and (not spoken or min(spoken) > response[0]),
              "Native pages must preserve song order and independent supplementary pages.")
    summary = visual.get("response_ending", {})
    omitted = set()
    if not any(features[role]["table"] for role in ("response", "ending")):
        omitted = {"table_style", "table_dimensions", "instrument_notation"}
        reasons = summary.get("not_applicable", {})
        e.require(isinstance(reasons, dict) and set(reasons) == omitted
                  and all(e.text(v) for v in reasons.values()), "Blank response/ending need table N/A reasons.")
    e.require(summary.get("page") == response[0] and e.text(summary.get("note"))
              and isinstance(summary.get("checks"), list)
              and artifacts.RESPONSE_CHECKS - omitted <= set(summary["checks"])
              and not omitted.intersection(summary["checks"]),
              "Inspect response/ending styles; explain absent tables in the observation.")
    return visual


def finalize(state, payload):
    e.safe_input(payload)
    exports = [x for x in state["exports"] if not x.get("retired_for_cleanup")]
    owner = str(Path(payload.get("path", "")).resolve())
    e.require(exports and exports[-1]["path"] == owner and unchanged(state, exports[-1])
              and exports[-1]["sha256"] == payload.get("sha256"),
              "Finalize only the latest unchanged mini export and its sources.")
    fingerprint, _, _ = binding(state)
    e.require(exports[-1]["binding"] == fingerprint, "Selection, source or implementation changed; export again.")
    inspection = payload.get("cleanup", {})
    e.require(isinstance(inspection, dict) and e.text(inspection.get("user_message"))
              and isinstance(inspection.get("checked_files"), list),
              "Record actual closed-file observations, including an empty list when no cleanup is needed.")
    working = copy.deepcopy(state)
    if payload.get("use_existing_review") is True:
        review = artifacts.accepted_review(working, owner)
        e.require(review and review["sha256"] == payload["sha256"], "No valid unchanged native review to reuse.")
    else:
        e.require(payload.get("confirmed") is True and e.text(payload.get("user_message"))
                  and {"layout", "colors", "content", "pagination"} <= set(payload.get("checks", [])),
                  "Record actual inspector observations, not a fabricated song/structure confirmation.")
        visual = visual_evidence(payload.get("visual"), exports[-1]["features"])
        review = {"path": owner, "sha256": payload["sha256"], "at": e.now(),
                  "review_version": 5, "visual": visual, "user_message": payload["user_message"],
                  "structural": renderer.inspect_docx(owner), "checks": payload["checks"]}
        working.setdefault("artifact_reviews", {})[owner] = review
    for old in working["exports"]:
        if old["path"] == owner or old.get("retired_for_cleanup"):
            continue
        e.require(unchanged(working, old), "Old export or sources changed/missing; preserve all files.")
        old.update(retired_for_cleanup=True, superseded_by=owner)
        for row in working["artifacts"]:
            if row["path"] in {old["path"], old["sources"]}:
                row.update(kind="obsolete", owner=owner)
    for row in working["artifacts"]:
        if row["kind"] in {"temporary", "obsolete"} and not row.get("deleted_at"):
            row["owner"] = owner
    plan = artifacts.cleanup(working, {})
    e.require(not plan["skipped"], "Cleanup preflight blocked; preserve changed, referenced or missing files.")
    expected = set(plan["candidates"])
    checks = inspection["checked_files"]
    registered = {row["path"]: row for row in working["artifacts"]}
    e.require(len(checks) == len(expected) and all(
        isinstance(row, dict) and row.get("path") in expected and row.get("closed") is True
        and row.get("sha256") == registered[row["path"]]["sha256"] for row in checks)
        and {row["path"] for row in checks} == expected,
        "Closed-file checks must exactly cover all deletion paths and registered hashes.")
    try:
        applied = artifacts.cleanup(working, {"apply": True})
        complete = not applied["skipped"] and not artifacts.remaining(working, owner)
        error = None
    except OSError:
        applied = {"deleted": [r["path"] for r in working["artifacts"]
                              if r["path"] in expected and r.get("deleted_at")]}
        complete, error = False, "cleanup_io_error"
    result = {"kind": "finalization", "version": 1, "path": owner, "sha256": review["sha256"],
              "complete": complete, "at": e.now(), "inspection": inspection,
              "preflight": plan, "deleted": applied["deleted"], "error": error}
    working.setdefault("cleanup_history", []).append(result)
    state.clear()
    state.update(working)
    return result


def status(state):
    active = [x for x in state["exports"] if not x.get("retired_for_cleanup")]
    current = active[-1] if active else None
    valid, warnings = False, []
    if state.get("selection"):
        try:
            fingerprint, _, warnings = binding(state)
            valid = bool(current and current["binding"] == fingerprint and unchanged(state, current))
        except WorkflowError as error:
            warnings = [{"status": "not_ready", "message": str(error)}]
        except (OSError, ValueError):
            warnings = [{"status": "stale", "message": "Source/template missing or changed."}]
    review = artifacts.accepted_review(state, current["path"]) if current and valid else {}
    reviewed = bool(review and review.get("sha256") == current["sha256"])
    finalized = bool(reviewed and artifacts.finalized(state, current["path"]))
    selection = state.get("selection")
    needs_download = bool(selection and any(
        song and key(song) not in state["results"]
        for song in [*selection["main"], selection["response"], selection["ending"]]))
    return {"mode": "mini", "revision": state["revision"], "selection": selection,
            "resources": state["results"], "current_export": current,
            "export_valid": valid, "visual_review_valid": reviewed, "finalization_valid": finalized,
            "delivery_complete": finalized, "content_complete": bool(valid and not warnings),
            "warnings": warnings, "cleanup_remaining": artifacts.remaining(state, current["path"]) if current else [],
            "next_action": ("select" if not selection else "download" if needs_download else "export" if not valid
                            else "artifact-finalize" if not finalized else "delivered")}
