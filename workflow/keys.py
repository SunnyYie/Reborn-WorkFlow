"""Existing-PCO-key recommendations, human audition approval and original MP3s."""

import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import engine as e
from pco_api import WorkflowError, identifier


def stale_spoken(state, reason):
    e.stale_setlist(state, reason)
    part = state.get("spoken")
    if not part:
        return
    if part.get("draft"):
        part["history"].append({
            "revision": part["revision"], "draft": part["draft"],
            "requirements": copy.deepcopy(part["requirements"]),
            "confirmation": part["confirmation"], "invalidated_by": reason,
        })
    part.update(stage="stale", draft=None, confirmation=None, revision=part["revision"] + 1)


def invalidate(state, reason):
    part = state.get("keys")
    if part:
        if part.get("proposals"):
            part["history"].append({
                "revision": part["revision"], "requirements": copy.deepcopy(part["requirements"]),
                "proposals": part["proposals"], "confirmation": part["confirmation"],
                "previews": part["previews"], "invalidated_by": reason,
            })
        part.update(revision=part["revision"] + 1, stage="ready_to_propose",
                    proposals=[], confirmation=None, previews={})
    # Keep document files/results, but a previous MP3 approval cannot survive new keys.
    for result in state["results"].values():
        result.pop("audio", None)
        if result.get("kind") == "numbered_score":
            previous = {k: result[k] for k in ("path", "sha256") if k in result}
            result.clear()
            result.update(status="needs_review", previous_document=previous,
                          message="Key requirements changed; recheck numbered score.")
    state["resource_reviews"] = {}
    stale_spoken(state, reason)
    state["stage"] = "collect_vocals"


def section(state):
    plan = e.confirmed_plan(state)
    part = state.get("keys")
    e.require(part and part["song_plan_digest"] == e.digest(plan),
              "Collect singer registers and male/female counts before selecting keys.")
    return part, plan


def requirements(state, payload):
    plan = e.confirmed_plan(state)
    e.safe_input(payload)
    e.require(set(payload) <= {"male_count", "female_count", "profiles", "notes"},
              "Unknown singer requirement fields.")
    old = state.get("keys")
    same = old and old["song_plan_digest"] == e.digest(plan)
    settings = dict(old["requirements"] if same else {}, **copy.deepcopy(payload))
    e.require(isinstance(settings.get("notes", ""), str), "Singer notes must be text.")
    for name in ("male_count", "female_count"):
        e.require(type(settings.get(name)) is int and settings[name] >= 0,
                  "Provide non-negative male_count and female_count.")
    e.require(settings["male_count"] + settings["female_count"] > 0, "At least one singer is required.")
    profiles = settings.get("profiles")
    e.require(isinstance(profiles, list) and bool(profiles), "Describe each singer group.")
    totals, seen = {"male": 0, "female": 0}, set()
    for profile in profiles:
        e.require(isinstance(profile, dict) and e.text(profile.get("id"))
                  and profile["id"] not in seen, "Singer group IDs must be unique.")
        seen.add(profile["id"])
        e.require(profile.get("gender") in totals, "Singer gender must be male or female.")
        e.require(profile.get("register") in ("low", "middle", "high"),
                  "Describe each group as low, middle or high.")
        e.require(type(profile.get("count")) is int and profile["count"] > 0, "Group count must be positive.")
        totals[profile["gender"]] += profile["count"]
        for field in ("comfortable_low", "comfortable_high"):
            e.require(profile.get(field) is None or e.text(profile[field]), "Optional range notes must be text.")
    e.require(totals == {"male": settings["male_count"], "female": settings["female_count"]},
              "Register-group counts must equal male/female totals.")
    invalidate(state, "singers_updated")
    part = state.get("keys") if same else {
        "revision": (old or {}).get("revision", 0) + 1, "history": [],
        "proposals": [], "confirmation": None, "previews": {}, "feedback": [],
    }
    if old and not same:
        part["history"] = copy.deepcopy(old.get("history", []))
    part.update(requirements=settings, song_plan_digest=e.digest(plan), stage="ready_to_propose")
    state["keys"] = part
    return part


def inventory(api, song):
    """Key details, unlike the Key collection, expose key-specific attachments."""
    root = f'/services/v2/songs/{identifier(song["song_id"])}/arrangements/{identifier(song["arrangement_id"])}'
    arrangement = api.request(root)["data"]
    attrs = arrangement["attributes"]
    e.require(not attrs.get("archived_at"), "Selected arrangement is archived.")
    e.require((attrs.get("bpm"), attrs.get("meter")) == (song["bpm"], song["meter"]),
              "Arrangement changed; inspect and reconfirm songs.")
    if song.get("source_updated_at") is not None:
        e.require(attrs.get("updated_at") == song["source_updated_at"], "Arrangement changed; reconfirm songs.")
    if song.get("lyrics_sha256") is not None:
        e.require(hashlib.sha256((attrs.get("lyrics") or "").encode()).hexdigest() == song["lyrics_sha256"],
                  "Lyrics changed; inspect and reconfirm songs.")
    rows = api.pages(arrangement.get("links", {}).get("keys") or root + "/keys")
    result = []
    for row in rows:
        key = api.request(row.get("links", {}).get("self") or root + "/keys/" + identifier(row["id"]))["data"]
        ka = key["attributes"]
        link = key.get("links", {}).get("attachments")
        audio = []
        if link:
            for attachment in api.pages(link):
                a = attachment["attributes"]
                filename = a.get("filename") or a.get("display_name") or ""
                if a.get("deleted_at") or Path(filename).suffix.lower() != ".mp3":
                    continue
                audio.append({
                    "attachment_id": attachment["id"], "filename": filename,
                    "downloadable": a.get("downloadable") is True,
                    "mp3_allowed": a.get("allow_mp3_download") is not False,
                    "updated_at": a.get("updated_at"), "size": a.get("file_size"),
                })
        start = ka.get("starting_key")
        label = (start + ("m" if ka.get("starting_minor") else "")) if start else ka.get("name")
        result.append({
            "key_id": key["id"], "label": label, "starting_key": start,
            "minor": ka.get("starting_minor"), "ending_key": ka.get("ending_key"),
            "ending_minor": ka.get("ending_minor"), "updated_at": ka.get("updated_at"),
            "audio_checked": bool(link), "audios": audio,
        })
    return result


def refresh(state, api):
    plan = e.confirmed_plan(state)
    catalog, errors = {}, []
    for song in plan["songs"]:
        try:
            catalog[song["song_id"]] = inventory(api, song)
        except WorkflowError as error:
            errors.append({"song_id": song["song_id"], "status": error.code, "message": str(error)})
            if error.code == "auth_error":
                break
    if state.get("key_catalog") and state["key_catalog"] != catalog:
        invalidate(state, "key_catalog_changed_or_unavailable")
    state["key_catalog"] = catalog
    return {"songs": catalog, "errors": errors}


def option(state, sid, kid, aid):
    key = next((k for k in state.get("key_catalog", {}).get(sid, []) if k["key_id"] == kid), None)
    e.require(key and e.text(key.get("label")), "Select an actual inspected PCO key.")
    audio = next((a for a in key["audios"] if a["attachment_id"] == aid), None)
    e.require(audio and audio["downloadable"] and audio["mp3_allowed"],
              "Choose a downloadable MP3 attached to this exact Key.")
    return key, audio


def propose(state, payload):
    part, plan = section(state)
    e.safe_input(payload)
    proposals = copy.deepcopy(payload.get("proposals"))
    e.require(isinstance(proposals, list) and 1 <= len(proposals) <= 3, "Provide one to three real key options.")
    singer_ids = {p["id"] for p in part["requirements"]["profiles"]}
    song_ids = [s["song_id"] for s in plan["songs"]]
    ids, signatures = set(), set()
    for proposal in proposals:
        e.require(e.text(proposal.get("id")) and proposal["id"] not in ids, "Use unique key proposal IDs.")
        ids.add(proposal["id"])
        e.require(e.text(proposal.get("reason")), "Explain the overall key recommendation.")
        songs = proposal.get("songs")
        e.require(isinstance(songs, list) and len(songs) == 3, "Choose keys for all three songs.")
        e.require([s.get("song_id") for s in songs] == song_ids, "Keep the confirmed song order.")
        for song in songs:
            for field in ("reason", "range_evidence"):
                e.require(e.text(song.get(field)), f"Explain {field}.")
            e.require(isinstance(song.get("lead_groups"), list) and bool(song["lead_groups"])
                      and set(song["lead_groups"]) <= singer_ids, "Identify the proposed lead singer group(s).")
            e.require(isinstance(song.get("unknowns"), list) and all(e.text(u) for u in song["unknowns"]),
                      "List unresolved vocal-range issues.")
            kid, aid = identifier(song.get("key_id")), identifier(song.get("attachment_id"))
            key, audio = option(state, song["song_id"], kid, aid)
            song.update(key_id=kid, attachment_id=aid, label=key["label"],
                        source_digest=e.digest({"key": key, "audio": audio}))
        signature = tuple((s["key_id"], s["attachment_id"]) for s in songs)
        e.require(signature not in signatures, "Do not pad with duplicate key proposals.")
        signatures.add(signature)
    e.require(payload.get("recommended_id") in ids, "Choose a recommended key proposal.")
    invalidate(state, "key_proposals_updated")
    part.update(proposals=proposals, recommended_id=payload["recommended_id"], stage="awaiting_audition",
                risk_notice="低/中/高音不等于实际可唱音域；调名与男女数量不能证明合适，须试听，建议试唱。")
    state["stage"] = "awaiting_key_audition"
    return part


def validate_mp3(content):
    tool = shutil.which("ffprobe")
    if not tool:
        raise WorkflowError("environment_error", "ffprobe is required to validate original MP3 files.")
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "audio.mp3"
        path.write_bytes(content)
        try:
            result = subprocess.run(
                [tool, "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mp3",
                 "-select_streams", "a", "-show_entries",
                 "stream=codec_name:format=duration", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=30, check=True,
            )
            data = json.loads(result.stdout)
            valid = (any(s.get("codec_name") == "mp3" for s in data.get("streams", []))
                     and float(data.get("format", {}).get("duration", 0)) > 0)
        except (subprocess.SubprocessError, OSError, ValueError, TypeError):
            valid = False
    if not valid:
        raise WorkflowError("invalid_file", "Downloaded content has no valid MP3 stream/duration.")


def verify_source(state, api, selection):
    _, plan = section(state)
    song = next(s for s in plan["songs"] if s["song_id"] == selection["song_id"])
    fresh = inventory(api, song)
    key = next((k for k in fresh if k["key_id"] == selection["key_id"]), None)
    audio = next((a for a in (key or {}).get("audios", [])
                  if a["attachment_id"] == selection["attachment_id"]), None)
    e.require(key and audio and audio["downloadable"] and audio["mp3_allowed"]
              and e.digest({"key": key, "audio": audio}) == selection["source_digest"],
              "Key/MP3 metadata changed or became restricted; refresh and audition again.")
    return audio


def file_valid(record, directory=None):
    path = Path(record["path"]) if record.get("path") else None
    return bool(path and path.is_file() and not path.is_symlink()
                and (directory is None or path.parent == Path(directory).resolve())
                and hashlib.sha256(path.read_bytes()).hexdigest() == record.get("sha256"))


def preview(state, payload, api, directory, save=lambda: None):
    from downloads import save_original
    part, _ = section(state)
    e.require(part["stage"] == "awaiting_audition", "Prepare auditions before approving keys; reuse existing files for replay.")
    proposal = next((p for p in part["proposals"] if p["id"] == payload.get("proposal_id")), None)
    e.require(proposal, "Unknown key proposal.")
    records = part["previews"].setdefault(proposal["id"], {})
    for selection in proposal["songs"]:
        sid = selection["song_id"]
        try:
            audio = verify_source(state, api, selection)
            previous = records.get(sid, {})
            if previous.get("source_digest") == selection["source_digest"] and file_valid(previous, directory):
                continue
            content = api.file(selection["attachment_id"])
            validate_mp3(content)
            path = save_original(Path(directory).resolve(), audio["filename"], content)
            records[sid] = {
                "status": "prepared", "path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
                "source_digest": selection["source_digest"], "label": selection["label"],
                "key_id": selection["key_id"], "attachment_id": selection["attachment_id"],
                "listened": False,
            }
        except WorkflowError as error:
            records[sid] = {"status": error.code, "message": str(error)}
            save()
            if error.code == "auth_error":
                break
        except OSError:
            records[sid] = {"status": "file_error", "message": "Cannot save audition MP3."}
        save()
    return records


def confirm(state, payload):
    part, _ = section(state)
    e.safe_input(payload)
    e.require(part["stage"] == "awaiting_audition", "No key proposal awaiting audition confirmation.")
    e.require(payload.get("revision") == part["revision"], "Stale key revision.")
    proposal = next((p for p in part["proposals"] if p["id"] == payload.get("proposal_id")), None)
    e.require(proposal and payload.get("confirmed") is True and payload.get("listened") is True
              and e.text(payload.get("user_message")), "Record actual human approval after listening to all three.")
    records = part["previews"].get(proposal["id"], {})
    e.require(all(records.get(s["song_id"], {}).get("source_digest") == s["source_digest"]
                  and file_valid(records[s["song_id"]]) for s in proposal["songs"]),
              "Prepare all three exact-key audition files before confirming.")
    unknowns = [u for s in proposal["songs"] for u in s["unknowns"]]
    e.require(not unknowns or payload.get("accept_unknowns") is True, "Disclose range uncertainties first.")
    part["confirmation"] = {
        "revision": part["revision"], "proposal_id": proposal["id"],
        "proposal_digest": e.digest(proposal), "requirements_digest": e.digest(part["requirements"]),
        "audition_sha256": {s["song_id"]: records[s["song_id"]]["sha256"] for s in proposal["songs"]},
        "listened": True, "user_message": payload["user_message"], "at": e.now(),
    }
    for selection in proposal["songs"]:
        records[selection["song_id"]]["listened"] = True
    part["stage"], state["stage"] = "confirmed", "keys_confirmed"
    return part


def confirmed(state):
    part, _ = section(state)
    confirmation = part.get("confirmation")
    e.require(part["stage"] == "confirmed" and confirmation
              and confirmation.get("listened") is True
              and confirmation["revision"] == part["revision"], "Approve keys after audition before downloading.")
    proposal = next((p for p in part["proposals"] if p["id"] == confirmation["proposal_id"]), None)
    e.require(proposal and e.digest(proposal) == confirmation["proposal_digest"]
              and e.digest(part["requirements"]) == confirmation["requirements_digest"],
              "Confirmed key plan or singer information changed.")
    return proposal


def feedback(state, payload):
    part, _ = section(state)
    e.safe_input(payload)
    e.require(part["proposals"] and e.text(payload.get("message")), "Record concrete key feedback.")
    part["feedback"].append({"revision": part["revision"], "message": payload["message"], "at": e.now()})
    invalidate(state, "key_feedback_received")
    return part


def deliver(state, api, directory, save=lambda: None):
    from downloads import save_original
    proposal = confirmed(state)
    part = state["keys"]
    records = part["previews"].get(proposal["id"], {})
    for selection in proposal["songs"]:
        sid = selection["song_id"]
        result = state["results"].setdefault(sid, {})
        previous = result.get("audio", {})
        if (previous.get("source_digest") == selection["source_digest"]
                and file_valid(previous, directory)):
            continue
        try:
            audio = verify_source(state, api, selection)
            sample = records.get(sid, {})
            content = (Path(sample["path"]).read_bytes() if sample.get("source_digest") == selection["source_digest"]
                       and file_valid(sample) else api.file(selection["attachment_id"]))
            if hashlib.sha256(content).hexdigest() != part["confirmation"]["audition_sha256"][sid]:
                raise WorkflowError("needs_review", "MP3 bytes differ from the auditioned file; audition and approve again.")
            validate_mp3(content)
            path = save_original(Path(directory).resolve(), audio["filename"], content)
            result["audio"] = {
                "status": "downloaded", "path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
                "source_digest": selection["source_digest"], "label": selection["label"],
                "key_id": selection["key_id"], "attachment_id": selection["attachment_id"],
            }
        except WorkflowError as error:
            result["audio"] = {"status": error.code, "message": str(error)}
        except OSError:
            result["audio"] = {"status": "file_error", "message": "Cannot save original MP3."}
        save()
        if result["audio"]["status"] == "auth_error":
            break
