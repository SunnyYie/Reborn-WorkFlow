"""Persistent conversation state and explicit confirmation barriers."""

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from pco_api import WorkflowError, identifier


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise WorkflowError("invalid_input", message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def safe_input(value):
    """Catch likely credentials and transient URLs before persisting agent inputs."""
    raw = json.dumps(value, ensure_ascii=False)
    require(not re.search(r"pco_pat_|[?&](?:X-Amz-|Signature=|token=)", raw, re.I),
            "Do not store credentials or signed URLs.")
    if isinstance(value, dict):
        for key, item in value.items():
            require(key.lower() not in {"secret", "client_secret", "authorization", "access_token"},
                    "Credential fields are forbidden.")
            safe_input(item)
    elif isinstance(value, list):
        for item in value:
            safe_input(item)


def new_state():
    return {
        "schema_version": 1, "session_id": uuid.uuid4().hex, "created_at": now(),
        "revision": 0, "stage": "collect_requirements", "requirements": {},
        "candidates": {}, "proposals": [], "confirmation": None,
        "feedback": [], "history": [], "results": {}, "resource_reviews": {},
    }


def ready(state):
    r = state["requirements"]
    return text(r.get("theme")) and len(r.get("slots", [])) == 3 and "preferences" in r


def stale_setlist(state, event):
    if state.get("setlist"):
        state["setlist"].update(stage="stale", confirmation=None, stale_reason=event)


def invalidate(state, event):
    stale_setlist(state, event)
    if state.get("keys"):
        import keys
        keys.invalidate(state, event)
        state["keys"]["song_plan_digest"] = None
    elif state.get("spoken"):
        part = state["spoken"]
        if part.get("draft") is not None:
            part["history"].append({
                "revision": part["revision"], "song_plan_digest": part["song_plan_digest"],
                "requirements": copy.deepcopy(part["requirements"]), "draft": part["draft"],
                "confirmation": part["confirmation"], "invalidated_by": event,
            })
        part["draft"] = None
        state["spoken"]["stage"] = "stale"
        state["spoken"]["confirmation"] = None
        state["spoken"]["revision"] += 1
        state["spoken"]["stale_reason"] = event
    state["key_catalog"] = {}
    if state["proposals"] or state["results"]:
        state["history"].append({
            "revision": state["revision"], "proposals": state["proposals"],
            "confirmation": state["confirmation"], "results": state["results"],
        })
    state["revision"] += 1
    state["proposals"] = []
    state["confirmation"] = None
    state["results"] = {}
    state["resource_reviews"] = {}
    state["stage"] = "ready_to_search" if ready(state) else (
        "collect_preferences" if text(state["requirements"].get("theme"))
        and len(state["requirements"].get("slots", [])) == 3 else "collect_requirements"
    )
    state["last_event"] = {"type": event, "at": now()}


def requirements(state, patch):
    require(isinstance(patch, dict), "Requirements must be an object.")
    require(set(patch) <= {"theme", "stages", "slots", "preferences", "mood", "notes"},
            "Unknown requirement fields.")
    safe_input(patch)
    result = dict(state["requirements"], **patch)
    if "theme" in result:
        require(text(result["theme"]), "Theme must not be empty.")
    if "slots" in result:
        require(isinstance(result["slots"], list) and len(result["slots"]) == 3
                and all(x in ("fast", "slow", "any") for x in result["slots"]),
                "Provide three positional slots: fast, slow or any.")
    for name in ("stages",):
        if name in result:
            require(isinstance(result[name], list) and all(text(x) for x in result[name]),
                    f"{name} must be a list of strings.")
    if "preferences" in result:
        require(isinstance(result["preferences"], list), "Preferences must be a list (or []).")
        for pref in result["preferences"]:
            require(isinstance(pref, dict) and text(pref.get("title")), "Preference needs title.")
            require(pref.get("mode", "preferred") in ("preferred", "required"),
                    "Preference mode must be preferred or required.")
            if pref.get("song_id"):
                identifier(pref["song_id"])
        require(sum(p.get("mode") == "required" for p in result["preferences"]) <= 3,
                "At most three songs can be mandatory.")
    state["requirements"] = result
    invalidate(state, "requirements_updated")
    return state


def add_candidates(state, rows):
    changed = False
    for row in rows:
        saved = {k: v for k, v in row.items() if k != "lyrics"}
        saved["lyrics_available"] = bool(row.get("lyrics"))
        saved["lyrics_sha256"] = hashlib.sha256(row.get("lyrics", "").encode()).hexdigest()
        key = f'{row["song_id"]}:{row["arrangement_id"]}'
        old = state["candidates"].get(key)
        if old and {k: v for k, v in old.items() if k != "checked_at"} != saved:
            changed = True
        saved["checked_at"] = now()
        state["candidates"][key] = saved
    if changed and state["proposals"]:
        invalidate(state, "candidate_evidence_changed")


def propose(state, payload):
    require(ready(state), "Collect theme, three slots and explicit preferences (including none) first.")
    safe_input(payload)
    plans = copy.deepcopy(payload.get("proposals"))
    require(isinstance(plans, list) and 1 <= len(plans) <= 3,
            "Submit one to three genuine proposals; never pad to three.")
    ids, orders = set(), set()
    mandatory = []
    for pref in state["requirements"]["preferences"]:
        if pref.get("mode") == "required":
            require(pref.get("song_id"), "Resolve mandatory song to a unique PCO ID first.")
            mandatory.append(str(pref["song_id"]))
    for plan in plans:
        require(text(plan.get("id")) and plan["id"] not in ids, "Proposal IDs must be unique.")
        ids.add(plan["id"])
        for field in ("label", "direction", "order_reason", "tradeoffs"):
            require(text(plan.get(field)), f"Proposal needs {field}.")
        require(isinstance(plan.get("songs"), list) and len(plan["songs"]) == 3,
                "Each proposal must contain exactly three songs.")
        order = []
        for position, song in enumerate(plan["songs"]):
            sid, aid = identifier(song.get("song_id")), identifier(song.get("arrangement_id"))
            candidate = state["candidates"].get(f"{sid}:{aid}")
            require(candidate, "Inspect the real PCO song/arrangement before recommending it.")
            song["song_id"], song["arrangement_id"] = sid, aid
            song["title"] = candidate["title"]
            song["bpm"], song["meter"] = candidate["bpm"], candidate["meter"]
            song["source_updated_at"] = candidate.get("updated_at")
            song["lyrics_sha256"] = candidate["lyrics_sha256"]
            for field in ("reason", "position_reason", "theme_evidence", "tempo_basis"):
                require(text(song.get(field)), f"Each song needs {field}.")
            require(song.get("evidence_kind") in (
                "current_lyrics", "historical_source", "known_version", "title_only"
            ), "Identify the theme evidence kind.")
            if song["evidence_kind"] == "current_lyrics":
                require(candidate["lyrics_available"], "Current lyrics are not available.")
            require(song.get("tempo") in ("fast", "slow", "unknown"), "Invalid tempo assessment.")
            slot = state["requirements"]["slots"][position]
            require(slot == "any" or song["tempo"] in (slot, "unknown"),
                    "Tempo conflicts with positional requirement; ask user to adjust it first.")
            unknowns = song.get("unknowns")
            require(isinstance(unknowns, list) and all(text(x) for x in unknowns),
                    "Provide unknowns as a list (or []).")
            if song["tempo"] == "unknown" or song["evidence_kind"] != "current_lyrics":
                require(bool(unknowns), "Unverified tempo/theme needs an explicit uncertainty.")
            song["candidate_fingerprint"] = digest({
                k: v for k, v in candidate.items() if k != "checked_at"
            })
            order.append(sid)
        require(len(set(order)) == 3, "A proposal cannot repeat the same song.")
        require(all(sid in order for sid in mandatory), "A mandatory song is missing.")
        require(tuple(order) not in orders, "Do not submit duplicate ordered song lists.")
        orders.add(tuple(order))
    require(payload.get("recommended_id") in ids, "Choose a recommended proposal ID.")
    invalidate(state, "proposals_replaced")
    state["proposals"], state["recommended_id"] = plans, payload["recommended_id"]
    state["stage"] = "awaiting_confirmation"
    return state


def feedback(state, payload):
    safe_input(payload)
    require(text(payload.get("message")), "Record the user's concrete optimization feedback.")
    require(state["proposals"], "No proposal to revise.")
    state["feedback"].append({"revision": state["revision"], "at": now(), **payload})
    invalidate(state, "feedback_received")
    return state


def confirm(state, payload):
    safe_input(payload)
    require(state["stage"] == "awaiting_confirmation", "No current proposal awaiting confirmation.")
    require(payload.get("revision") == state["revision"], "Confirmation is for a stale revision.")
    require(payload.get("confirmed") is True and text(payload.get("user_message")),
            "Record explicit human confirmation, not an inferred preference.")
    plan = next((p for p in state["proposals"] if p["id"] == payload.get("proposal_id")), None)
    require(plan, "Unknown proposal.")
    uncertainties = [u for song in plan["songs"] for u in song["unknowns"]]
    require(not uncertainties or payload.get("accept_unknowns") is True,
            "Disclose uncertainties and obtain explicit acceptance before confirmation.")
    state["confirmation"] = {
        "revision": state["revision"], "proposal_id": plan["id"], "plan_digest": digest(plan),
        "user_message": payload["user_message"], "accepted_unknowns": uncertainties, "at": now(),
    }
    state["stage"] = "collect_vocals"
    return state


def confirmed_plan(state):
    c = state.get("confirmation")
    require(c and c["revision"] == state["revision"], "Downloads require current confirmation.")
    plan = next((p for p in state["proposals"] if p["id"] == c["proposal_id"]), None)
    require(plan and digest(plan) == c["plan_digest"], "Confirmed song order changed.")
    return plan
