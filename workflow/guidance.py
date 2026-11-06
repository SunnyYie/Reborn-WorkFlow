"""Read-only orchestration hints. Existing validators remain the authority."""

import engine as e
import keys
from pco_api import WorkflowError
import setlist
import spoken
import status


def step(action, commands, actor="agent", missing=None, note=None):
    return {
        "action": action, "commands": commands, "actor": actor,
        "missing": missing or [], "note": note,
        "automatic_confirmation_allowed": False,
    }


def valid(check, state):
    try:
        return check(state)
    except (WorkflowError, ValueError, OSError, KeyError, TypeError):
        return None


def next_step(state):
    """Never mutate state, fetch resources, create approvals or infer listening."""
    requirements = state.get("requirements", {})
    missing = [key for key in ("theme", "slots", "preferences") if key not in requirements]
    if missing:
        return step("collect_requirements", ["requirements"], "user", missing,
                    "Ask only for missing business information, not commands or technical steps.")
    plan = valid(e.confirmed_plan, state)
    if not plan:
        if state.get("stage") == "awaiting_confirmation":
            return step("confirm_songs", ["confirm", "feedback"], "user",
                        note="Present the current proposals and revision; wait for actual approval.")
        return step("research_and_propose", ["rag-search", "catalog", "inspect", "propose"],
                    note="Read source lyrics and Skills; do not infer theme matching from titles.")
    part = state.get("keys", {})
    keyplan = valid(keys.confirmed, state)
    if not keyplan:
        if not part.get("requirements") or part.get("stage") == "stale":
            return step("collect_vocals", ["key-requirements"], "user")
        if part.get("stage") != "awaiting_audition":
            return step("propose_keys", ["key-options", "key-propose"])
        proposals = part.get("proposals", [])
        prepared = any(all(
            part.get("previews", {}).get(p["id"], {}).get(s["song_id"], {}).get("source_digest")
            == s["source_digest"]
            and keys.file_valid(part.get("previews", {}).get(p["id"], {}).get(s["song_id"], {}))
            for s in p["songs"]) for p in proposals)
        return (step("listen_and_confirm_keys", ["key-confirm", "key-feedback"], "user",
                     note="Play the exact files. Prepared/decoded does not mean a human listened.")
                if prepared else step("prepare_audition", ["key-preview"]))
    results = state.get("results", {})
    pending = []
    previews = part.get("previews", {}).get(keyplan["id"], {})
    for song in keyplan["songs"]:
        sid = song["song_id"]
        result = results.get(sid, {})
        audio = result.get("audio", {})
        if (result.get("status") not in {"downloaded", "reused", "missing"}
                or audio.get("status") != "downloaded" or not keys.file_valid(audio)
                or audio.get("sha256") != previews.get(sid, {}).get("sha256")):
            pending.append(sid)
    if pending:
        return step("prepare_and_download_resources",
                    ["rag-search", "material-prepare", "resources", "review-resources", "download"],
                    missing=pending, note="Missing, restricted, ambiguous and failed are different outcomes.")
    unprepared = [s["song_id"] for s in plan["songs"]
                  if not valid(lambda st, sid=s["song_id"]: setlist.material(st, sid), state)]
    if unprepared:
        return step("prepare_materials", ["rag-search", "material-prepare"], missing=unprepared,
                    note="Use verified originals. Unreadable scores/audio require a capable reviewer.")
    speech = state.get("spoken", {})
    if not valid(spoken.render, state):
        if speech.get("stage") == "awaiting_confirmation":
            return step("confirm_spoken", ["spoken-confirm", "spoken-feedback"], "user")
        return step("prepare_spoken", ["spoken-context", "spoken-requirements", "spoken-propose"],
                    note="Verify scripture and timing evidence before presenting the draft.")
    assembled = state.get("setlist", {})
    current = valid(setlist.current, state)
    if not current:
        return step("prepare_setlist", ["setlist-propose"],
                    note="Collect response/ending choices and current event information; verify scripture.")
    if assembled.get("stage") == "awaiting_confirmation":
        return step("review_and_confirm_setlist", ["setlist-export", "setlist-confirm"], "user",
                    note="Export a draft and inspect it in native WPS/Word; wait for content approval.")
    delivery = status.summary(state)
    if delivery["delivery_complete"]:
        return step("complete", [], note="Document delivery is not proof of rehearsal acceptance.")
    if not delivery["current_artifact"]:
        return step("export_setlist", ["setlist-export"])
    if not delivery["artifact_bytes_valid"]:
        return step("repair_artifact", [], "agent",
                    note="Existing artifact/source bytes changed. Investigate; do not mark reviewed.")
    return step("native_review_and_finalize", ["artifact-finalize"], "capable_reviewer",
                note="Actually inspect native pagination and closed-file state. Never fabricate observations.")
