"""Read-only current-state summary; historical prose never becomes active workflow state."""
from pathlib import Path

import artifacts
import engine as e
import keys
from pco_api import WorkflowError
import setlist
import spoken


def summary(state):
    phases = {}
    for name, check in (("songs", e.confirmed_plan), ("keys", keys.confirmed),
                        ("spoken", spoken.render), ("setlist", setlist.current)):
        try:
            check(state)
            part = state if name == "songs" else state.get(name, {})
            phases[name] = {"valid": True, "stage": part.get("stage"),
                            "revision": part.get("revision"),
                            "confirmed": bool(part.get("confirmation"))}
        except (WorkflowError, ValueError, OSError, KeyError, TypeError):
            part = state if name == "songs" else state.get(name, {})
            phases[name] = {"valid": False, "stage": part.get("stage", "not_started"),
                            "revision": part.get("revision"), "confirmed": False}
    current = state.get("setlist", {})
    exports = [row for row in current.get("exports", []) if not row.get("retired_for_cleanup")]
    latest = exports[-1] if exports else None
    review = state.get("artifact_reviews", {}).get(latest["path"], {}) if latest else {}
    valid_file = bool(latest and Path(latest["path"]).is_file()
                      and not Path(latest["path"]).is_symlink()
                      and artifacts.sha(latest["path"]) == latest["sha256"]
                      and artifacts.sources_valid(state, latest["path"]))
    reviewed = bool(valid_file and artifacts.accepted_review(state, latest["path"])
                    and review.get("sha256") == latest["sha256"])
    remaining = artifacts.remaining(state, latest["path"] if latest else None)
    finalized = bool(reviewed and latest and artifacts.finalized(state, latest["path"]))
    cleanup_required = bool(latest and latest.get("required_cleanup_version"))
    ready = (all(p["valid"] and p["confirmed"] for p in phases.values()) and reviewed
             and latest["status"] == "exported" and not state.get("test_run") and not remaining
             and (not cleanup_required or finalized))
    return {
        "session_id": state["session_id"], "phases": phases,
        "current_artifact": latest, "artifact_bytes_valid": valid_file,
        "visual_review_valid": reviewed,
        "visual_review_format": review.get("review_version", 1) if review else None,
        "required_review_version": latest.get("required_review_version", 1) if latest else None,
        "finalization_required": cleanup_required, "finalization_valid": finalized,
        "next_action": ("artifact-finalize" if reviewed and (remaining or cleanup_required and not finalized)
                        else "artifact-review_or_finalize" if latest and not reviewed else None),
        "cleanup_remaining": remaining, "delivery_complete": ready,
        "notice": "Read-only delivery status, not rehearsal acceptance. New exports require full style and page isolation acceptance.",
    }
