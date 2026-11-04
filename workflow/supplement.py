"""Optional response-song audition and original-resource delivery, separate from main three."""
import copy
import hashlib
from pathlib import Path

import engine as e
import keys


def binding(state):
    return e.digest({"plan": e.confirmed_plan(state), "keys": keys.confirmed(state),
                     "vocalists": state["keys"]["requirements"]})


def current(state):
    part = state.get("supplement", {})
    e.require(part.get("binding") == binding(state),
              "Prepare supplemental response resources for the current song/vocalist plan.")
    return part


def options(state, payload, api):
    e.safe_input(payload)
    sid, aid = str(payload.get("song_id")), str(payload.get("arrangement_id"))
    song = next((s for s in api.inspect(sid) if str(s["arrangement_id"]) == aid), None)
    e.require(song, "Choose an existing PCO response arrangement.")
    song = copy.deepcopy(song)
    if "lyrics" in song:
        song["lyrics_available"] = bool(song["lyrics"])
        song["lyrics_sha256"] = hashlib.sha256(song.pop("lyrics").encode()).hexdigest()
    part = {"song": copy.deepcopy(song), "catalog": keys.inventory(api, song),
            "binding": binding(state), "confirmation": None}
    state["supplement"] = part
    e.stale_setlist(state, "response_song_updated")
    return part


def source(part, payload, api):
    catalog = keys.inventory(api, part["song"])
    key = next((k for k in catalog if k["key_id"] == str(payload.get("key_id"))), None)
    audio = next((a for a in (key or {}).get("audios", [])
                  if a["attachment_id"] == str(payload.get("attachment_id"))
                  and a["downloadable"] and a["mp3_allowed"]), None)
    e.require(key and audio, "Choose a downloadable original MP3 of an existing PCO key.")
    return key, audio, e.digest({"key": key, "audio": audio})


def preview(state, payload, api, directory):
    e.safe_input(payload)
    from downloads import save_original
    part = current(state)
    key, audio, digest = source(part, payload, api)
    content = api.file(audio["attachment_id"])
    keys.validate_mp3(content)
    path = save_original(Path(directory), audio["filename"], content)
    part["preview"] = {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
                       "source_digest": digest, "key_id": key["key_id"],
                       "attachment_id": audio["attachment_id"], "label": key["label"]}
    part["confirmation"] = None
    e.stale_setlist(state, "response_audition_updated")
    return part["preview"]


def confirm(state, payload, api, directory):
    e.safe_input(payload)
    from downloads import save_original
    part = current(state)
    sample = part.get("preview", {})
    e.require(keys.file_valid(sample) and payload.get("sha256") == sample["sha256"]
              and payload.get("confirmed") is True and payload.get("listened") is True
              and e.text(payload.get("user_message")),
              "Explicitly approve this exact response MP3 after listening.")
    _, audio, digest = source(part, sample, api)
    e.require(digest == sample["source_digest"], "Response MP3 metadata changed; audition again.")
    content = Path(sample["path"]).read_bytes()
    keys.validate_mp3(content)
    path = save_original(Path(directory), audio["filename"], content)
    part["audio"] = {**sample, "path": str(path), "status": "downloaded"}
    part["confirmation"] = {"binding": binding(state), "sha256": sample["sha256"],
                            "user_message": payload["user_message"], "listened": True, "at": e.now()}
    e.stale_setlist(state, "response_key_confirmed")
    return part["audio"]


def approved(state, sid):
    part = current(state)
    audio = part.get("audio", {})
    approval = part.get("confirmation", {})
    e.require(str(part["song"]["song_id"]) == str(sid)
              and approval.get("binding") == binding(state) and approval.get("listened") is True
              and audio.get("status") == "downloaded" and keys.file_valid(audio)
              and audio["sha256"] == approval.get("sha256"),
              "Supplemental response requires its own unchanged audition-approved delivered MP3.")
    return audio


def resources(state, payload, api, directory):
    """Reuse Chinese-first/score-second selection; do not synthesize lyrics or audio."""
    import downloads
    e.safe_input(payload)
    part = current(state)
    audio = approved(state, part["song"]["song_id"])
    import setlist
    reused = setlist.reused(state, str(part["song"]["song_id"]))
    if reused:
        return reused
    song = dict(part["song"], selected_key_id=audio["key_id"])
    sources = downloads.inventory(api, song)
    if payload.get("inspect_only") is True:
        return {"sources": downloads.public_inventory(sources)}
    supplied = payload.get("review", {})
    e.require(isinstance(supplied, dict), "Resource review must be an object.")
    by_id = {s["id"]: s for s in sources}
    reviews = {}
    for row in supplied.get("classifications", []):
        item = by_id.get(row.get("source_id"))
        e.require(item and row.get("kind") in {downloads.CHINESE_LYRICS, downloads.SCORE, "other"}
                  and e.text(row.get("evidence")), "Response classification needs current source and evidence.")
        if item["id"].startswith("lyrics:") and item["kind"] == downloads.CHINESE_LYRICS:
            e.require(row["kind"] == downloads.CHINESE_LYRICS, "Do not reclassify original Chinese lyrics.")
        reviews[item["id"]] = {"kind": row["kind"], "evidence": row["evidence"],
                               "fingerprint": downloads.source_fingerprint(item)}
    choice = supplied.get("selected_source_id")
    e.require(choice is None or choice in by_id and e.text(supplied.get("user_message")),
              "Record an explicit response document/version choice.")
    review = {"classifications": reviews, "selected_source_id": choice,
              "choice_fingerprint": downloads.source_fingerprint(by_id[choice]) if choice else None}
    selected = downloads.choose(sources, review, audio["key_id"])
    e.require(selected is not None, "No downloadable Chinese lyrics or numbered score for response song.")
    content = (selected["text"].encode("utf-8") if selected.get("text") is not None
               else api.file(selected["attachment_id"]))
    downloads.validate_file(content, selected["filename"], selected["kind"])
    path = downloads.save_original(Path(directory), selected["filename"], content)
    part["document"] = {"status": "downloaded", "kind": selected["kind"], "path": str(path),
                        "sha256": hashlib.sha256(content).hexdigest()}
    return part["document"]
