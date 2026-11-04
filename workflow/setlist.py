"""Session-bound RAG materials and final five-slot worship documents."""

import copy
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3

import engine as e
import keys
import spoken
import artifacts
import supplement
from pco_api import WorkflowError

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setlist_rag", ROOT / "rag" / "rag.py")
rag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rag)
DEFAULT_DB = ROOT / "rag" / "data" / "songs.sqlite3"


def record(dbpath, rid):
    db = rag.connect(dbpath)
    try:
        return rag.get_record(db, rid)
    finally:
        db.close()


def fingerprint(r):
    return e.digest(r)


def search(state, title, dbpath=DEFAULT_DB):
    result = rag.resolve(dbpath, title)
    state.setdefault("retrieval", {})[title] = {
        "database": str(Path(dbpath).resolve()), "result": result, "at": e.now(),
    }
    return result


def working_db(directory, base=DEFAULT_DB):
    path = Path(directory) / "materials" / "songs.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with closing(sqlite3.connect(base)) as src, closing(sqlite3.connect(path)) as dest:
            src.backup(dest)
    return path


def same_title(title, r):
    titles = [r["title"], *r.get("aliases", [])]
    chinese = lambda s: "".join(re.findall(r"[\u3400-\u9fff]", s))
    return any(rag.norm(title) == rag.norm(t) or
               (chinese(title) and chinese(title) == chinese(t)) for t in titles)


def song_for_material(state, sid):
    plan = e.confirmed_plan(state)
    song = next((s for s in plan["songs"] if s["song_id"] == sid), None)
    if not song:
        part = supplement.current(state)
        e.require(str(part["song"]["song_id"]) == sid, "Material does not belong to this plan or response.")
        supplement.approved(state, sid)
        song = part["song"]
    return song


def material(state, sid):
    plan = e.confirmed_plan(state)
    song = song_for_material(state, sid)
    e.require(song, "Material must belong to the confirmed song plan.")
    entry = state.get("materials", {}).get(sid)
    e.require(entry and entry["song_plan_digest"] == e.digest(plan),
              "Prepare current song materials first.")
    r = record(entry["database"], entry["id"])
    e.require(fingerprint(r) == entry["fingerprint"], "Material changed; prepare and confirm again.")
    e.require(not r["issues"] and r["role"] == "main", "Unresolved or incomplete material.")
    for citation in r.get("locator", {}).get("analysis", {}).get("evidence", []):
        if "audio" in citation:
            e.require(keys.file_valid(citation["audio"]), "Analysis audio evidence changed; verify again.")
    return r, entry


def prepare(state, payload, directory, base=DEFAULT_DB):
    e.safe_input(payload)
    plan = e.confirmed_plan(state)
    sid = str(payload.get("song_id"))
    song = song_for_material(state, sid)
    e.require(song and e.text(payload.get("version_basis")),
              "Identify a confirmed song and explain version identity.")
    dbpath = working_db(directory, base)
    if payload.get("id"):
        r = record(dbpath, payload["id"])
        e.require(same_title(song["title"], r), "RAG title/alias does not match the PCO song.")
        origin = "rag"
    else:
        search_result = rag.resolve(dbpath, song["title"])
        e.require(search_result["status"] == "miss",
                  "RAG has candidates; select/review the existing version before using a new document.")
        result = (state.get("results", {}).get(sid, {}) if
                  sid in {s["song_id"] for s in plan["songs"]}
                  else supplement.current(state).get("document", {}))
        analysis = payload.get("analysis")
        e.require(result.get("status") == "downloaded" and
                  result.get("kind") in {"chinese_lyrics", "numbered_score"},
                  "New material requires an original downloaded Chinese lyric document or score.")
        path = Path(result.get("path", ""))
        e.require(path.is_file() and not path.is_symlink() and
                  hashlib.sha256(path.read_bytes()).hexdigest() == result.get("sha256"),
                  "Downloaded lyric source is missing or changed.")
        if analysis is not None:
            e.require(isinstance(analysis, dict) and analysis.get("confirmed") is True
                      and e.text(analysis.get("user_message"))
                      and analysis.get("source_sha256") == result["sha256"]
                      and analysis.get("unknowns") == [],
                      "Analysis requires exact source hash, explicit verification and no unresolved unknowns.")
            text = "\n".join(line for section in payload.get("lyric_sections", [])
                             for line in section.get("lines", []))
        elif path.suffix.lower() == ".txt" and result["kind"] == "chinese_lyrics":
            text = path.read_text(encoding="utf-8")
        elif path.suffix.lower() == ".docx" and result["kind"] == "chinese_lyrics":
            from docx import Document
            doc = Document(path)
            text = "\n".join([p.text for p in doc.paragraphs] +
                             [c.text for t in doc.tables for row in t.rows for c in row.cells])
        else:
            raise WorkflowError("needs_review", "Score/image/DOC needs explicit verified analysis; no automatic transcription.")
        sections = payload.get("lyric_sections")
        route = payload.get("route")
        e.require(isinstance(sections, list) and sections and isinstance(route, list) and route,
                  "Provide source-grounded lyric sections and a proposed route.")
        e.require(e.text(payload.get("structure_basis")), "Explain how sections and route were determined.")
        if analysis is not None:
            evidence = analysis.get("evidence")
            e.require(isinstance(evidence, list) and evidence, "Analysis needs page/time evidence.")
            targets = set()
            for item in evidence:
                e.require(isinstance(item, dict) and e.text(item.get("target"))
                          and (type(item.get("page")) is int and item["page"] > 0
                               or type(item.get("start_seconds")) in (int, float)
                               and item["start_seconds"] >= 0
                               and type(item.get("end_seconds")) in (int, float)
                               and item["end_seconds"] > item["start_seconds"]
                               and isinstance(item.get("audio"), dict)
                               and keys.file_valid(item["audio"])),
                          "Each analysis citation needs a positive page or a hash-verified timed audio segment.")
                targets.add(item["target"])
            required = {"lyrics:" + s["label"] for s in sections}
            required.update("route:" + str(i) for i in range(len(route)))
            e.require(required <= targets, "Cite every lyric section and route occurrence, including bars/LL/Tag.")
        for section in sections:
            e.require(isinstance(section, dict) and e.text(section.get("label")) and
                      isinstance(section.get("lines"), list) and section["lines"],
                      "Every section needs a label and original lines.")
            if analysis is None and rag.marker(section["label"]) in {"LL", "TAG"}:
                e.require(re.search(r"(?im)^\s*" + re.escape(section["label"]) + r"\s*$", text),
                          "Do not invent LL/Tag absent an explicit source section.")
            for line in section["lines"]:
                e.require(e.text(line) and rag.norm(line) in rag.norm(text),
                          "Lyric line does not occur in the downloaded original.")
        for item in route:
            e.require(isinstance(item, dict) and e.text(item.get("label")),
                      "Every route item needs a label.")
        source_id = result["sha256"][:24]
        snapshot = dbpath.parent / (source_id + path.suffix.lower())
        if not snapshot.exists():
            snapshot.write_bytes(path.read_bytes())
        e.require(rag.sha(snapshot) == result["sha256"], "Material snapshot collision.")
        r = {
            "id": e.digest({"source": source_id, "sections": sections, "route": route})[:24],
            "source_id": source_id, "source_name": path.name, "title": song["title"],
            "role": "main", "aliases": [], "blocks": [],
            "locator": {"kind": "verified_analysis" if analysis else "downloaded_lyrics",
                        "structure_basis": payload["structure_basis"],
                        **({"analysis": copy.deepcopy(analysis)} if analysis else {})},
            "route": copy.deepcopy(route), "lyric_sections": copy.deepcopy(sections),
            "issues": [], "evidence": "downloaded_lyrics_proposed_structure",
            "current_event_confirmed": False, "meter": song["meter"],
            "historical_key": None, "historical_bpm": None,
        }
        rag.validate_record(r)
        allowed = {rag.marker(s["label"]) for s in sections}
        for item in route:
            label = rag.marker(item["label"])
            e.require(label in allowed or label in {"IN", "INS", "INT", "OUT", "OUTRO", "END"},
                      "Route contains a section without corresponding lyrics.")
        with closing(rag.connect(dbpath)) as db, db:
            db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?)",
                       (source_id, str(path), str(snapshot), path.name, result["sha256"]))
            db.execute("INSERT OR IGNORE INTO songs VALUES (?,?,?,?,?,?)",
                       (r["id"], source_id, r["title"], "main", rag.dump(r),
                        rag.vector(r["title"] + " " + text)))
        r = record(dbpath, r["id"])
        origin = "verified_analysis" if analysis else "pco_download"
    rag.validate_record(r)
    e.require(r["role"] == "main" and not r["issues"], "Select a reviewed complete main record.")
    entry = {
        "id": r["id"], "database": str(dbpath), "fingerprint": fingerprint(r),
        "origin": origin, "song_plan_digest": e.digest(plan),
        "version_basis": payload["version_basis"], "at": e.now(),
    }
    state.setdefault("materials", {})[sid] = entry
    e.stale_setlist(state, "material_updated")
    state.setdefault("setlist", {})["stage"] = "stale"
    return entry


def reused(state, sid):
    if sid not in state.get("materials", {}):
        return None
    r, entry = material(state, sid)
    return {"status": "reused", "kind": "chinese_lyrics", "record_id": r["id"],
            "database": entry["database"], "material_fingerprint": entry["fingerprint"]}


def spoken_context(state):
    """Read prepared evidence before writing speech; never select a version or infer tempo."""
    plan = e.confirmed_plan(state)
    result = []
    for position, song in enumerate(plan["songs"], 1):
        entry = state.get("materials", {}).get(song["song_id"])
        if not entry:
            result.append({"song_position": position, "song_id": song["song_id"],
                           "status": "material_not_prepared"})
            continue
        r, _ = material(state, song["song_id"])
        result.append({"song_position": position, "song_id": song["song_id"],
                       "record_id": r["id"], "source_name": r["source_name"],
                       "material_fingerprint": entry["fingerprint"],
                       "interludes": rag.interludes(r), "status": "prepared"})
    return {"songs": result, "timing_notice": "Historical bars are evidence, not live tempo or rehearsal acceptance."}


def context_digest(state):
    plan = e.confirmed_plan(state)
    keyplan = keys.confirmed(state)
    delivery = []
    for selected in keyplan["songs"]:
        sid = selected["song_id"]
        audio = state.get("results", {}).get(sid, {}).get("audio", {})
        e.require(audio.get("status") == "downloaded" and keys.file_valid(audio)
                  and audio.get("source_digest") == selected["source_digest"]
                  and audio.get("key_id") == selected["key_id"]
                  and audio.get("attachment_id") == selected["attachment_id"]
                  and audio.get("sha256") == state["keys"]["confirmation"]["audition_sha256"][sid],
                  "Complete workflow requires delivery of all three unchanged audition-confirmed MP3s.")
        delivery.append(audio)
    spoken.render(state)
    materials = {s["song_id"]: material(state, s["song_id"])[1] for s in plan["songs"]}
    return e.digest({"plan": plan, "keys": keyplan, "spoken": state["spoken"]["confirmation"],
                     "materials": materials, "delivery": delivery})


def scripture(value):
    e.require(isinstance(value, dict), "Supply a freshly checked scripture record.")
    for field in ("reference", "translation", "quote", "source"):
        e.require(e.text(value.get(field)), "Scripture needs reference, translation, quote and source.")
    e.require(value.get("verification") in {"source_checked", "user_provided"},
              "Unchecked scriptures cannot enter the complete setlist.")
    return value


def route_override(r, supplied):
    route = supplied.get("route")
    if route is None:
        return {}
    e.require(isinstance(route, list) and route and e.text(supplied.get("structure_basis")),
              "Current arrangement needs a nonempty route and musician/verified analysis basis.")
    allowed = {rag.marker(s["label"]) for s in r["lyric_sections"]}
    for item in route:
        e.require(isinstance(item, dict) and set(item) <= {"label", "cue", "current_cue"}
                  and e.text(item.get("label"))
                  and rag.marker(item["label"]) in allowed | {"IN", "INS", "INT", "OUT", "OUTRO", "END"}
                  and isinstance(item.get("cue", ""), str)
                  and isinstance(item.get("current_cue", ""), str),
                  "Route must use existing lyric sections/instrumentals with explicit current cues.")
    return {"route": copy.deepcopy(route), "structure_basis": supplied["structure_basis"]}


def validate_spoken_routes(state, main):
    for slot in state["spoken"]["requirements"]["slots"]:
        if slot["placement"] != "interlude" or slot.get("window") is None or "route_index" not in slot:
            continue
        position = slot["song_position"] - 1
        supplied = main[position]
        if "route" not in supplied:
            continue
        route, index = supplied["route"], slot["route_index"]
        e.require(isinstance(route, list) and 0 <= index < len(route)
                  and isinstance(route[index], dict)
                  and rag.marker(route[index].get("label", "")) in {"IN", "INS", "INT"},
                  "Current route moved the speaking interlude; rebind and confirm speech first.")
        item = route[index]
        match = re.match(r"^\s*(\d+)(?:\s|[^\d]|$)", item.get("current_cue", item.get("cue", "")))
        e.require(not match or int(match.group(1)) == slot["window"]["bars"],
                  "Current route bars disagree with the confirmed speech window.")


def propose(state, payload):
    e.safe_input(payload)
    context = context_digest(state)
    plan = e.confirmed_plan(state)
    keyplan = keys.confirmed(state)
    specs = payload.get("main")
    e.require(isinstance(specs, list) and len(specs) == 3, "Supply scriptures for three ordered songs.")
    validate_spoken_routes(state, specs)
    selection = {"main": []}
    presentation = payload.get("presentation", {})
    e.require(isinstance(presentation, dict) and set(presentation) <= {"show_event_metadata"}
              and type(presentation.get("show_event_metadata", False)) is bool,
              "presentation supports only boolean show_event_metadata.")
    selection["presentation"] = {"show_event_metadata": presentation.get("show_event_metadata", False)}
    event = payload.get("event")
    if event is not None:
        e.require(isinstance(event, dict) and set(event) <= {"title", "date", "personnel", "basis"}
                  and e.text(event.get("basis")) and
                  all(isinstance(v, str) for v in event.values()), "Use current event text and its source basis.")
        selection["event"] = copy.deepcopy(event)
    dbpaths = set()
    for song, k, supplied in zip(plan["songs"], keyplan["songs"], specs):
        e.require(supplied.get("song_id") == song["song_id"], "Keep confirmed main song order.")
        r, entry = material(state, song["song_id"])
        dbpaths.add(entry["database"])
        verse = scripture(supplied.get("scripture"))
        e.require(verse["translation"] == state["spoken"]["requirements"]["bible_translation"],
                  "Setlist and spoken scripture translations must agree.")
        bpm = supplied.get("bpm", "")
        e.require(bpm == "" or type(bpm) in (int, float) and 0 < bpm < 400,
                  "BPM must be blank or a musician-supplied positive number.")
        e.require(bpm == "" or e.text(supplied.get("bpm_basis")), "BPM requires musician evidence.")
        selection["main"].append({
            "id": r["id"], "key": k["label"], "bpm": bpm,
            "event_metadata_basis": "Current audition-confirmed Key ID " + k["key_id"],
            "bpm_basis": supplied.get("bpm_basis", ""),
            "scripture": verse["reference"] + "（" + verse["translation"] + "）\n" + verse["quote"],
            "scripture_basis": copy.deepcopy(verse),
            **route_override(r, supplied),
        })
    e.require(len(dbpaths) == 1, "All material must use the same session database.")
    dbpath = dbpaths.pop()
    for role in ("response", "ending"):
        supplied = payload.get(role)
        e.require(isinstance(supplied, dict), "Choose response and ending songs explicitly.")
        allowed_fields = ({"song_id", "main_id", "scripture", "key", "bpm", "bpm_basis",
                           "route", "structure_basis"} if role == "response"
                          else {"song_id", "main_id", "id"})
        e.require(set(supplied) <= allowed_fields, "Unsupported response/ending fields must not be silently ignored.")
        sid = str(supplied.get("song_id"))
        if sid in state["materials"]:
            r, entry = material(state, sid)
        else:
            e.require(supplied.get("main_id"), "Choose a prepared song or an explicit RAG main_id.")
            r = record(dbpath, supplied["main_id"])
            e.require(r["role"] == "main" and not r["issues"],
                      "A new response/ending song requires complete reviewed main lyrics.")
        if role == "response":
            main_spec = next((x for x in selection["main"] if x["id"] == r["id"]), None)
            inherited_key = main_spec["key"] if main_spec else None
            requested_key = supplied.get("key", inherited_key)
            if main_spec and requested_key == inherited_key:
                key_label = inherited_key
                key_basis = main_spec["event_metadata_basis"]
            else:
                audio = supplement.approved(state, sid)
                e.require(same_title(supplement.current(state)["song"]["title"], r),
                          "Response material and auditioned PCO title/alias must match.")
                e.require(requested_key in (None, audio["label"]), "Use the audition-approved response key.")
                key_label = audio["label"]
                key_basis = "Response audition-confirmed Key ID " + audio["key_id"]
            bpm = supplied.get("bpm", main_spec.get("bpm", "") if main_spec else "")
            e.require(bpm == "" or type(bpm) in (int, float) and 0 < bpm < 400
                      and (e.text(supplied.get("bpm_basis")) or
                           main_spec and bpm == main_spec.get("bpm")),
                      "Response BPM must be blank or supported by current musician evidence.")
            selection[role] = {"id": r["id"], "key": key_label, "bpm": bpm,
                               "event_metadata_basis": key_basis,
                               "bpm_basis": supplied.get("bpm_basis", main_spec.get("bpm_basis", "") if main_spec else ""),
                               **route_override(r, supplied)}
            if not main_spec:
                verse = scripture(supplied.get("scripture"))
                e.require(verse["translation"] == state["spoken"]["requirements"]["bible_translation"],
                          "Use the same scripture translation for a new response song.")
                selection[role].update(
                    scripture=verse["reference"] + "（" + verse["translation"] + "）\n" + verse["quote"],
                    scripture_basis=verse)
            elif "scripture" in supplied:
                verse = scripture(supplied["scripture"])
                e.require(verse["translation"] == state["spoken"]["requirements"]["bible_translation"],
                          "Use the same scripture translation.")
                selection[role].update(scripture=verse["reference"] + "（" + verse["translation"] + "）\n" + verse["quote"],
                                       scripture_basis=verse)
            if not main_spec or "route" in supplied or "scripture" in supplied or (
                    key_label != inherited_key or bpm != main_spec["bpm"]):
                selection[role]["full_structure"] = True
            if not main_spec or key_label != inherited_key:
                selection[role]["audio"] = copy.deepcopy(supplement.approved(state, sid))
                selection[role]["supplement_binding"] = supplement.binding(state)
        else:
            result = rag.ending_plan(dbpath, r["title"], r["id"], supplied.get("id"),
                                     store_proposal=True)
            e.require(result["status"] in {"reuse", "proposed"},
                      "Ending has multiple versions or unresolved coverage; choose/review first.")
            ending = result["record"]
            selection[role] = {"id": ending["id"]}
    old = state.get("setlist", {})
    history = copy.deepcopy(old.get("history", []))
    if old.get("selection"):
        history.append({k: copy.deepcopy(v) for k, v in old.items() if k != "history"})
    part = {"revision": old.get("revision", 0) + 1, "stage": "awaiting_confirmation",
            "context_digest": context, "database": dbpath, "selection": selection,
            "record_fingerprints": {x["id"]: fingerprint(record(dbpath, x["id"]))
                                    for x in [*selection["main"], selection["response"], selection["ending"]]},
            "confirmation": None, "exports": [], "history": history}
    state["setlist"] = part
    return part


def current(state):
    part = state.get("setlist", {})
    e.require(part.get("context_digest") == context_digest(state) and part.get("stage") != "stale",
              "Setlist context changed; propose again.")
    for rid, saved in part["record_fingerprints"].items():
        e.require(fingerprint(record(part["database"], rid)) == saved,
                  "Setlist source changed; reselect and confirm again.")
    response = part["selection"]["response"]
    if response.get("audio"):
        audio = supplement.approved(state, supplement.current(state)["song"]["song_id"])
        e.require(audio == response["audio"] and response["supplement_binding"] == supplement.binding(state),
                  "Response audition changed; propose and confirm again.")
    return part


def confirm(state, payload):
    e.safe_input(payload)
    part = current(state)
    e.require(part["stage"] == "awaiting_confirmation" and payload.get("revision") == part["revision"],
              "No current setlist revision awaiting confirmation.")
    e.require(payload.get("confirmed") is True and e.text(payload.get("user_message")),
              "Record actual explicit setlist confirmation.")
    part["confirmation"] = {"selection_digest": e.digest(part["selection"]),
                            "user_message": payload["user_message"], "at": e.now()}
    part["stage"] = "confirmed"
    return part


def export(state, output, draft=False, reuse=False):
    part = current(state)
    e.require(draft or part["stage"] == "confirmed" and part["confirmation"] and
              part["confirmation"]["selection_digest"] == e.digest(part["selection"]),
              "Only confirmed setlists can be formally exported; use draft for review.")
    selection = copy.deepcopy(part["selection"])
    selection["spoken"] = copy.deepcopy(state["spoken"]["draft"]["cues"])
    slots = {s["id"]: s for s in state["spoken"]["requirements"]["slots"]}
    for cue in selection["spoken"]:
        cue["song_position"] = slots[cue["slot_id"]]["song_position"]
    selection["test_run"] = bool(state.get("test_run"))
    selection["confirmed"] = not draft
    selection["workflow"] = {
        "session_id": state["session_id"], "theme": state["requirements"]["theme"],
        "song_plan_digest": state["confirmation"]["plan_digest"],
        "spoken_confirmation": state["spoken"]["confirmation"],
        "key_selection": keys.confirmed(state)["songs"],
        "materials": copy.deepcopy(state["materials"]),
        "assembly_confirmation": part["confirmation"] if not draft else None,
    }
    # Exact repeated requests reuse only unchanged, registered bytes and sources.
    request_digest = e.digest({
        "selection": selection, "records": part["record_fingerprints"],
        "renderer": rag.sha(ROOT / "rag/template_renderer.py"),
        "assembler": rag.sha(ROOT / "rag/rag.py"), "template": rag.sha(
            ROOT / "skills/pco-assemble-worship-setlist/assets/reference.docx"),
    })
    for previous in reversed(part["exports"]) if reuse else []:
        if (previous.get("request_digest") == request_digest and not previous.get("retired_for_cleanup")
                and Path(previous["path"]).is_file() and not Path(previous["path"]).is_symlink()
                and rag.sha(previous["path"]) == previous["sha256"]
                and artifacts.sources_valid(state, previous["path"])):
            return {**previous, "reused": True}
    pending = [row for row in state.get("artifacts", [])
               if row.get("required_cleanup_version") and not row.get("deleted_at")
               and artifacts.accepted_review(state, row["path"])
               and (not artifacts.finalization_record(state, row["path"])
                    or part["exports"] and part["exports"][-1]["path"] == row["path"]
                    and not artifacts.finalized(state, row["path"]))]
    e.require(not pending,
              "Finalize the accepted formal export immediately with artifact-finalize before another export.")
    result = rag.assemble(part["database"], selection, output)
    result["status"] = "test_artifact" if state.get("test_run") else ("draft" if draft else "exported")
    result["sha256"] = rag.sha(result["path"])
    result["request_digest"] = request_digest
    if not draft:
        result["required_cleanup_version"] = artifacts.FINALIZATION_VERSION
    part["exports"].append(result)
    registered = artifacts.register(state, result["path"], "final")
    registered["required_review_version"] = result["required_review_version"]
    if not draft:
        registered["required_cleanup_version"] = artifacts.FINALIZATION_VERSION
    artifacts.register(state, result["sources"], "sidecar", result["path"])
    return result
