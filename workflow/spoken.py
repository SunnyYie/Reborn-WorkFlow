"""Phase two: song-bound prayers, Scripture and spoken transitions."""

import copy
import math
import re

import engine as e
import keys


def default_slots():
    return [
        {"id": "song1_interlude", "song_position": 1, "placement": "interlude",
         "kind": "prayer", "scripture_required": False, "target_seconds": None},
        {"id": "song2_before_intro", "song_position": 2, "placement": "before_intro",
         "kind": "words", "scripture_required": True, "target_seconds": None},
        {"id": "song3_before_intro", "song_position": 3, "placement": "before_intro",
         "kind": "words", "scripture_required": True, "target_seconds": None},
        {"id": "song3_interlude", "song_position": 3, "placement": "interlude",
         "kind": "words", "scripture_required": False, "target_seconds": None},
    ]


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def window_seconds(slot):
    window = slot.get("window")
    if window is None:
        return None
    e.require(slot["placement"] == "interlude" and isinstance(window, dict)
              and set(window) == {"bars", "beats_per_bar", "bpm", "reserve_seconds",
                                  "basis", "tempo_basis"},
              "Interlude window needs bars, explicit tempo beats per bar, BPM, reserve and evidence.")
    e.require(type(window["bars"]) is int and window["bars"] > 0
              and positive(window["beats_per_bar"]) and positive(window["bpm"])
              and type(window["reserve_seconds"]) in (int, float)
              and math.isfinite(window["reserve_seconds"]) and window["reserve_seconds"] >= 0
              and e.text(window["basis"])
              and window["tempo_basis"] in {"reference_only", "musician"},
              "Use finite positive timing values and distinguish reference tempo from musician tempo.")
    available = window["bars"] * window["beats_per_bar"] * 60 / window["bpm"] - window["reserve_seconds"]
    e.require(available > 0, "Interlude reserve consumes the whole window.")
    return available


def validate_timing_context(state, part):
    # Legacy confirmations stay readable; all newly collected requirements use this gate.
    if part.get("timing_policy_version", 0) < 2:
        return
    import setlist
    plan = e.confirmed_plan(state)
    for slot in part["requirements"]["slots"]:
        if slot["placement"] != "interlude":
            continue
        sid = plan["songs"][slot["song_position"] - 1]["song_id"]
        if sid not in state.get("materials", {}):
            continue
        record, entry = setlist.material(state, sid)
        occurrences = setlist.rag.interludes(record)
        if not any(item["bars"] is not None for item in occurrences) and "route_index" not in slot:
            continue
        e.require(type(slot.get("route_index")) is int
                  and slot.get("material_fingerprint") == entry["fingerprint"],
                  "Bind the speaking interlude to the prepared material fingerprint and route_index.")
        item = next((item for item in occurrences if item["route_index"] == slot["route_index"]), None)
        e.require(item, "Speaking route_index must identify a stored instrumental occurrence.")
        window = slot.get("window")
        if window is None:
            e.require(e.text(slot.get("timing_unknown_reason")),
                      "Supply an estimated interlude window or explicitly explain unknown tempo/timing.")
        elif item["bars"] is not None:
            e.require(window["bars"] == item["bars"] or e.text(slot.get("bar_override_basis")),
                      "Do not change stored bars without explicit current user/musician evidence.")


def section(state):
    plan = e.confirmed_plan(state)
    keyplan = keys.confirmed(state)
    result = state.get("spoken")
    e.require(result and result["stage"] != "stale", "Collect phase-two requirements first.")
    e.require(result["song_plan_digest"] == e.digest(plan),
              "Song plan changed; rebuild spoken requirements and draft.")
    e.require(result.get("key_plan_digest") == e.digest(keyplan),
              "Keys changed; rebuild spoken requirements and draft.")
    return result, plan


def archive(part):
    if part["draft"] is not None:
        part["history"].append({
            "revision": part["revision"], "requirements": copy.deepcopy(part["requirements"]),
            "draft": part["draft"], "confirmation": part["confirmation"],
        })


def reset(part, stage):
    part["revision"] += 1
    part["draft"] = None
    part["confirmation"] = None
    part["stage"] = stage


def requirements(state, payload):
    plan = e.confirmed_plan(state)
    keyplan = keys.confirmed(state)
    e.safe_input(payload)
    e.require(set(payload) <= {
        "tone", "audience", "bible_translation", "slots", "notes",
        "bridge_previous", "avoid_title_announcements",
    }, "Unknown spoken requirement field.")
    old = state.get("spoken")
    same_plan = (old and old["stage"] != "stale" and old["song_plan_digest"] == e.digest(plan)
                 and old.get("key_plan_digest") == e.digest(keyplan))
    settings = copy.deepcopy(old["requirements"] if same_plan else {
        "tone": "温柔、真诚、适合口述", "audience": "会众",
        "bible_translation": "和合本", "slots": default_slots(), "notes": "",
        "bridge_previous": True, "avoid_title_announcements": True,
    })
    settings.update(copy.deepcopy(payload))
    for key in ("tone", "audience", "bible_translation"):
        e.require(e.text(settings[key]), f"Provide {key}.")
    e.require(isinstance(settings["notes"], str), "notes must be text.")
    for key in ("bridge_previous", "avoid_title_announcements"):
        e.require(type(settings[key]) is bool, f"{key} must be boolean.")
    slots = settings["slots"]
    e.require(isinstance(slots, list) and bool(slots), "At least one speaking location is required.")
    seen = set()
    for slot in slots:
        e.require(isinstance(slot, dict), "Each speaking location must be an object.")
        e.require(e.text(slot.get("id")) and slot["id"] not in seen, "Location IDs must be unique.")
        seen.add(slot["id"])
        e.require(type(slot.get("song_position")) is int and slot["song_position"] in (1, 2, 3),
                  "song_position must be 1, 2 or 3.")
        e.require(slot.get("placement") in ("before_intro", "interlude"), "Invalid placement.")
        e.require(slot.get("kind") in ("prayer", "words"), "Invalid speaking kind.")
        e.require(type(slot.get("scripture_required")) is bool, "Specify scripture_required.")
        e.require(slot.get("target_seconds") is None or positive(slot["target_seconds"]),
                  "target_seconds must be a positive estimate or null.")
        window_seconds(slot)
    part = copy.deepcopy(old) if same_plan else {
        "revision": (old or {}).get("revision", 0), "song_plan_digest": e.digest(plan),
        "key_plan_digest": e.digest(keyplan),
        "song_revision": state["revision"], "stage": "collect_requirements",
        "requirements": {}, "draft": None, "confirmation": None,
        "feedback": [], "history": [], "exports": [],
    }
    if old and not same_plan:
        part["history"] = copy.deepcopy(old.get("history", []))
        if old.get("draft"):
            part["history"].append({
                "revision": old["revision"], "song_plan_digest": old["song_plan_digest"],
                "requirements": old["requirements"], "draft": old["draft"],
                "confirmation": old["confirmation"],
            })
    archive(part)
    part["requirements"] = settings
    part["timing_policy_version"] = 2
    reset(part, "ready_to_draft")
    state["spoken"] = part
    e.stale_setlist(state, "spoken_requirements_updated")
    return part


def context(plan, slot):
    index = slot["song_position"] - 1
    current = plan["songs"][index]
    previous = plan["songs"][index - 1] if index and slot["placement"] == "before_intro" else None
    return {
        "song_id": current["song_id"], "song_title": current["title"],
        "previous_song_id": previous["song_id"] if previous else None,
        "previous_song_title": previous["title"] if previous else None,
    }


def propose(state, payload):
    part, plan = section(state)
    e.safe_input(payload)
    validate_timing_context(state, part)
    cues = copy.deepcopy(payload.get("cues"))
    e.require(isinstance(cues, list), "Provide cues for the requested locations.")
    slots = part["requirements"]["slots"]
    by_id = {slot["id"]: slot for slot in slots}
    e.require(len(cues) == len(slots), "Cover every requested location exactly once.")
    seen = set()
    for cue in cues:
        e.require(isinstance(cue, dict), "Each cue must be an object.")
        slot = by_id.get(cue.get("slot_id"))
        e.require(slot and cue["slot_id"] not in seen, "Unknown or duplicate speaking location.")
        seen.add(cue["slot_id"])
        for key in ("opening", "body", "closing", "song_connection", "delivery_note"):
            e.require(e.text(cue.get(key)), f"Each cue needs {key}.")
        ctx = context(plan, slot)
        if ctx["previous_song_id"] and part["requirements"]["bridge_previous"]:
            e.require(e.text(cue.get("previous_connection")),
                      "Connect previous song content without reciting its title or full lyrics.")
        cue.update(ctx)
        cue["placement"], cue["kind"] = slot["placement"], slot["kind"]
        cue["text"] = "\n\n".join(cue[key] for key in ("opening", "body", "closing"))
        if slot["placement"] == "before_intro" and part["requirements"]["avoid_title_announcements"]:
            # A title such as "让我" is also ordinary speech; do not ban bare substrings.
            for song in plan["songs"]:
                aliases = {song["title"], *re.findall(r"[\u3400-\u9fff]{2,}", song["title"])}
                aliases |= {alias.replace("祢", "你") for alias in aliases}
                for alias in aliases:
                    title = re.escape(alias)
                    e.require(not re.search(
                        r"《\s*" + title + r"\s*》|(?:唱|歌名[是为為：:]?)\s*[“\"「]?" + title,
                        cue["closing"],
                    ), "Close with the next song's meaning, not a song-title announcement.")
        e.require(positive(cue.get("estimated_seconds")), "Provide a positive estimated speaking time.")
        if slot.get("target_seconds") is not None:
            e.require(cue["estimated_seconds"] <= slot["target_seconds"],
                      "Estimated speech exceeds the requested window; shorten it.")
        available = window_seconds(slot)
        if available is not None:
            e.require(cue["estimated_seconds"] <= available,
                      "Estimated speech plus reserve exceeds the interlude; shorten it, never extend bars or rush.")
        cue["timing"] = {"available_seconds": available,
                         "status": "estimated_not_rehearsed" if available is not None else "window_unknown",
                         "window": copy.deepcopy(slot.get("window"))}
        scriptures = cue.get("scriptures")
        e.require(isinstance(scriptures, list), "Provide scriptures as a list (or []).")
        e.require(not slot["scripture_required"] or scriptures, "Scripture is required at this location.")
        for scripture in scriptures:
            e.require(isinstance(scripture, dict), "Scripture entries must be objects.")
            for key in ("reference", "translation", "source"):
                e.require(e.text(scripture.get(key)), f"Scripture needs {key}.")
            e.require(scripture["translation"] == part["requirements"]["bible_translation"],
                      "Use the requested Bible translation consistently.")
            e.require(scripture.get("mode") in ("quotation", "paraphrase", "reference_only"),
                      "Separate verbatim quotation, paraphrase and reference.")
            e.require(scripture.get("verification") in ("source_checked", "user_provided", "unverified"),
                      "Record Scripture verification provenance.")
            if scripture["mode"] == "quotation":
                e.require(e.text(scripture.get("quote")) and scripture["quote"] in cue["text"],
                          "The original Scripture quotation must appear verbatim in the spoken text.")
            else:
                e.require(not scripture.get("quote"), "Do not label paraphrase as a verbatim quotation.")
        e.require(isinstance(cue.get("unknowns"), list) and all(e.text(u) for u in cue["unknowns"]),
                  "List unresolved song/timing/context issues in unknowns (or []).")
        if slot.get("timing_unknown_reason"):
            e.require(slot["timing_unknown_reason"] in cue["unknowns"],
                      "Disclose the unresolved interlude timing in the cue's unknowns.")
    archive(part)
    reset(part, "awaiting_confirmation")
    part["draft"] = {
        "cues": sorted(cues, key=lambda c: list(by_id).index(c["slot_id"])),
        "created_at": e.now(), "song_plan_digest": e.digest(plan),
    }
    e.stale_setlist(state, "spoken_draft_updated")
    return part


def feedback(state, payload):
    part, _ = section(state)
    e.safe_input(payload)
    e.require(part["draft"] is not None, "No spoken draft to revise.")
    e.require(e.text(payload.get("message")), "Record concrete spoken-script feedback.")
    part["feedback"].append({"revision": part["revision"], "at": e.now(), "message": payload["message"]})
    archive(part)
    reset(part, "ready_to_draft")
    e.stale_setlist(state, "spoken_feedback")
    return part


def confirm(state, payload):
    part, _ = section(state)
    e.safe_input(payload)
    validate_timing_context(state, part)
    e.require(part["stage"] == "awaiting_confirmation", "No spoken draft awaiting confirmation.")
    e.require(payload.get("revision") == part["revision"], "Stale spoken revision.")
    e.require(payload.get("confirmed") is True and e.text(payload.get("user_message")),
              "Record explicit human approval of the spoken script, separately from song approval.")
    cues = part["draft"]["cues"]
    e.require(not any(s["verification"] == "unverified" for c in cues for s in c["scriptures"]),
              "Verify Scripture references/quotations before finalizing; acceptance cannot bypass this.")
    unknowns = [u for c in cues for u in c["unknowns"]]
    e.require(not unknowns or payload.get("accept_unknowns") is True,
              "Disclose unresolved issues and obtain acceptance first.")
    part["confirmation"] = {
        "revision": part["revision"], "draft_digest": e.digest(part["draft"]),
        "requirements_digest": e.digest(part["requirements"]),
        "user_message": payload["user_message"], "accepted_unknowns": unknowns, "at": e.now(),
    }
    part["stage"] = "confirmed"
    return part


def render(state):
    part, plan = section(state)
    validate_timing_context(state, part)
    confirmation = part["confirmation"]
    e.require(part["stage"] == "confirmed" and confirmation
              and confirmation["revision"] == part["revision"]
              and confirmation["draft_digest"] == e.digest(part["draft"])
              and confirmation["requirements_digest"] == e.digest(part["requirements"]),
              "Export requires the currently confirmed spoken script.")
    lines = ["# 敬拜祷告与话语", "",
             "歌序：" + " → ".join(s["title"] for s in plan["songs"]), "",
             "定调：" + " → ".join(s["label"] for s in keys.confirmed(state)["songs"]), "",
             "口述时长仅为估计，须按实际前奏/间奏排练；不包含自动播放或乐队调度。", ""]
    labels = {"before_intro": "前奏开始前", "interlude": "间奏"}
    for cue in part["draft"]["cues"]:
        lines.extend([
            f'## {cue["song_title"]} · {labels[cue["placement"]]}', "",
            f'类型：{cue["kind"]}；口述估计：{cue["estimated_seconds"]} 秒', "",
            cue["text"], "", "现场提示：" + cue["delivery_note"], "",
        ])
        for verse in cue["scriptures"]:
            lines.extend([
                f'经文：{verse["reference"]}（{verse["translation"]}；{verse["mode"]}）',
                f'核对来源：{verse["source"]}；{verse["verification"]}', "",
            ])
        if cue["unknowns"]:
            lines.extend(["已披露待核实项：" + "；".join(cue["unknowns"]), ""])
    return "\n".join(lines)
