"""Conservative resource discovery and original-file delivery. No transcription."""

import hashlib
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from engine import confirmed_plan, digest, require, safe_input, text
from pco_api import WorkflowError, identifier

CHINESE_LYRICS = "chinese_lyrics"
SCORE = "numbered_score"
DOCUMENT_EXTENSIONS = {".txt", ".docx", ".doc", ".pdf", ".png", ".jpg", ".jpeg"}


def chinese(value):
    # Avoid treating a Chinese title preceding English-only lyrics as Chinese lyrics.
    lines = [line for line in value.splitlines() if re.search(r"[\u3400-\u9fff]", line)]
    count = len(re.findall(r"[\u3400-\u9fff]", value))
    return count >= 20 or (len(lines) >= 2 and count >= 10)


def filename_kind(filename):
    name = filename.lower()
    if re.search(r"中文歌[词詞]|chinese[ _-]*lyrics", name):
        return CHINESE_LYRICS
    if re.search(r"[简簡][谱譜]|jianpu|numbered[ _-]*(?:score|notation)", name):
        return SCORE
    if re.search(r"chord|和[弦絃]|五[线線][谱譜]|staff|lead[ _-]*sheet", name):
        return "other"
    if re.search(r"lyrics", name) and not re.search(r"[\u3400-\u9fff]", name):
        return "other"
    return "unknown"


def source_fingerprint(source):
    return digest({k: v for k, v in source.items() if k != "text"})


def inventory(api, song):
    """Read all active sources; no /open calls or file transfers."""
    sid = identifier(song["song_id"])
    root = f"/services/v2/songs/{sid}"
    current = api.request(root)["data"]
    if current["attributes"].get("hidden"):
        raise WorkflowError("needs_review", "Confirmed song is now hidden.")
    arrangements = api.pages(root + "/arrangements?per_page=100")
    selected = next((a for a in arrangements if a["id"] == song["arrangement_id"]), None)
    if selected is None or selected["attributes"].get("archived_at"):
        raise WorkflowError("needs_review", "Confirmed arrangement is unavailable or archived.")
    expected = (song["bpm"], song["meter"])
    actual = (selected["attributes"].get("bpm"), selected["attributes"].get("meter"))
    if expected != actual:
        raise WorkflowError("needs_review", "Confirmed arrangement tempo/meter changed; reconfirm plan.")
    if (song.get("source_updated_at") is not None
            and selected["attributes"].get("updated_at") != song["source_updated_at"]):
        raise WorkflowError("needs_review", "Confirmed arrangement was updated; inspect and reconfirm.")
    if (song.get("lyrics_sha256") is not None and hashlib.sha256(
            (selected["attributes"].get("lyrics") or "").encode()
    ).hexdigest() != song["lyrics_sha256"]):
        raise WorkflowError("needs_review", "Confirmed lyrics changed; inspect and reconfirm.")
    sources, seen = [], set()

    def attachments(path, scope, aid=None, key_id=None):
        for row in api.pages(path):
            attrs = row["attributes"]
            if attrs.get("deleted_at") or row["id"] in seen:
                continue
            seen.add(row["id"])
            filename = attrs.get("filename") or attrs.get("display_name") or ""
            if Path(filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
                continue
            sources.append({
                "id": "attachment:" + row["id"], "attachment_id": row["id"],
                "filename": filename, "kind": filename_kind(filename), "scope": scope,
                "arrangement_id": aid, "downloadable": attrs.get("downloadable") is True,
                "updated_at": attrs.get("updated_at"), "size": attrs.get("file_size"),
                "key_id": key_id,
            })

    # Selected arrangement comes first, so shared attachment IDs retain that scope.
    ordered = sorted(arrangements, key=lambda a: a["id"] != song["arrangement_id"])
    for row in ordered:
        attrs, aid = row["attributes"], row["id"]
        if attrs.get("archived_at"):
            continue
        scope = "selected" if aid == song["arrangement_id"] else "other_arrangement"
        lyrics = attrs.get("lyrics") or ""
        if re.search(r"[\u3400-\u9fff]", lyrics):
            sources.append({
                "id": "lyrics:" + aid,
                "kind": CHINESE_LYRICS if chinese(lyrics) else "unknown", "scope": scope,
                "arrangement_id": aid, "filename": song["title"] + "-中文歌词.txt",
                "downloadable": True, "text": lyrics,
                "text_sha256": hashlib.sha256(lyrics.encode()).hexdigest(),
                "updated_at": attrs.get("updated_at"),
            })
        base = f"{root}/arrangements/{identifier(aid)}"
        attachments(row.get("links", {}).get("attachments") or base + "/attachments", scope, aid)
        for key in api.pages(row.get("links", {}).get("keys") or base + "/keys"):
            link = key.get("links", {}).get("attachments")
            if not link and key.get("links", {}).get("self"):
                link = api.request(key["links"]["self"])["data"].get("links", {}).get("attachments")
            if link:
                attachments(link, scope, aid, key["id"])
    attachments(current.get("links", {}).get("attachments") or root + "/attachments", "song")
    return sources


def public_inventory(sources):
    return [{k: v for k, v in s.items() if k != "text"} | {
        "fingerprint": source_fingerprint(s)
    } for s in sources]


def review_resources(state, payload, api):
    import keys
    plan = confirmed_plan(state)
    keyplan = keys.confirmed(state)
    safe_input(payload)
    sid = identifier(payload.get("song_id"))
    song = next((s for s in plan["songs"] if s["song_id"] == sid), None)
    require(song, "Only review a confirmed song.")
    song = dict(song, selected_key_id=next(s["key_id"] for s in keyplan["songs"] if s["song_id"] == sid))
    sources = inventory(api, song)
    by_id = {s["id"]: s for s in sources}
    reviews = {}
    for row in payload.get("classifications", []):
        source = by_id.get(row.get("source_id"))
        require(source, "Unknown resource source.")
        require(row.get("kind") in (CHINESE_LYRICS, SCORE, "other"), "Invalid resource kind.")
        require(text(row.get("evidence")), "Resource classification needs evidence; no guessing.")
        if source["id"].startswith("lyrics:") and source["kind"] == CHINESE_LYRICS:
            require(row["kind"] == CHINESE_LYRICS, "Do not reclassify original Chinese lyrics.")
        reviews[source["id"]] = {
            "kind": row["kind"], "evidence": row["evidence"],
            "fingerprint": source_fingerprint(source),
        }
    choice = payload.get("selected_source_id")
    if choice is not None:
        require(choice in by_id and text(payload.get("user_message")),
                "Record explicit human choice for ambiguous resource/version.")
    state["resource_reviews"][sid] = {
        "classifications": reviews, "selected_source_id": choice,
        "choice_fingerprint": source_fingerprint(by_id[choice]) if choice else None,
        "user_message": payload.get("user_message"),
    }
    return {"song_id": sid, "review_saved": True}


def choose(sources, review, selected_key_id=None):
    sources = [s for s in sources if not (
        selected_key_id and s["kind"] == SCORE and s.get("key_id")
        and s["key_id"] != selected_key_id
    )]
    fingerprints = {s["id"]: source_fingerprint(s) for s in sources}
    sources = [dict(s) for s in sources]
    for source in sources:
        record = review.get("classifications", {}).get(source["id"])
        if record and record["fingerprint"] == source_fingerprint(source):
            source["kind"] = record["kind"]
    sources = [s for s in sources if not (
        selected_key_id and s["kind"] == SCORE and s.get("key_id")
        and s["key_id"] != selected_key_id
    )]
    choice = review.get("selected_source_id")
    chosen = next((s for s in sources if s["id"] == choice), None)
    if chosen and review.get("choice_fingerprint") != fingerprints[choice]:
        raise WorkflowError("needs_review", "Chosen resource changed; review it again.")
    lyrics = [s for s in sources if s["kind"] == CHINESE_LYRICS]
    unknown = [s for s in sources if s["kind"] == "unknown"]
    if lyrics:
        pool = lyrics
    elif unknown:
        raise WorkflowError("needs_review", "Unclassified documents may contain Chinese lyrics.")
    else:
        pool = [s for s in sources if s["kind"] == SCORE]
    if choice:
        if not chosen or chosen not in pool:
            raise WorkflowError("needs_review", "Chosen source is unavailable or violates lyrics priority.")
        if not chosen["downloadable"]:
            raise WorkflowError("restricted", "Chosen resource is not downloadable.")
        return chosen
    if not pool:
        return None
    priority = {"selected": 0, "song": 1, "other_arrangement": 2}
    best = min(priority[s["scope"]] for s in pool)
    pool = [s for s in pool if priority[s["scope"]] == best]
    field = [s for s in pool if s["id"].startswith("lyrics:")]
    if field:
        pool = field
    if len(pool) != 1 or best == 2:
        raise WorkflowError("needs_review", "Multiple resources or a different arrangement; ask user to choose.")
    if not pool[0]["downloadable"]:
        raise WorkflowError("restricted", "Chinese lyrics/score exists but download is restricted.")
    return pool[0]


def validate_file(data, filename, kind):
    ext = Path(filename).suffix.lower()
    if not data or data.lstrip().lower().startswith((b"<html", b"<!doctype", b"{", b"[")):
        raise WorkflowError("invalid_file", "Empty file, HTML or JSON is not a lyric/score file.")
    valid = False
    if ext == ".pdf":
        valid = data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:]
    elif ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                valid = {"[Content_Types].xml", "word/document.xml"} <= set(archive.namelist())
                if valid and kind == CHINESE_LYRICS:
                    info = archive.getinfo("word/document.xml")
                    if info.file_size > 10 * 1024 * 1024:
                        valid = False
                    else:
                        document = ElementTree.fromstring(archive.read(info))
                        words = "\n".join(
                            n.text or "" for n in document.iter()
                            if n.tag.endswith("}t")
                        )
                        valid = bool(re.search(r"[\u3400-\u9fff]", words))
        except ImportError:
            raise WorkflowError("environment_error", "Python XML runtime is broken; use ./run.") from None
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError):
            valid = False
    elif ext == ".doc":
        valid = data.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
    elif ext == ".png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext in (".jpg", ".jpeg"):
        valid = data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    elif ext == ".txt":
        try:
            decoded = data.decode("utf-8-sig")
            valid = bool(decoded.strip()) and (
                kind != CHINESE_LYRICS or bool(re.search(r"[\u3400-\u9fff]", decoded))
            )
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise WorkflowError("invalid_file", "File signature/content does not match the expected resource.")


def save_original(directory, filename, content):
    directory.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename).strip(". ") or "resource"
    name = Path(filename)
    # Exclusive creation never follows an existing destination symlink or overwrites a file.
    for number in range(10000):
        path = directory / (filename if not number else f"{name.stem}-{number}{name.suffix}")
        if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
            return path.resolve()
        try:
            with path.open("xb") as handle:
                try:
                    handle.write(content)
                    handle.flush()
                except OSError:
                    path.unlink(missing_ok=True)
                    raise
            return path.resolve()
        except FileExistsError:
            continue
    raise WorkflowError("file_error", "Too many conflicting destination names.")


def download(state, api, directory, save=lambda: None):
    import keys
    import setlist
    plan = confirmed_plan(state)
    keyplan = keys.confirmed(state)
    key_by_song = {s["song_id"]: s for s in keyplan["songs"]}
    directory = Path(directory).resolve()
    for song in plan["songs"]:
        sid = song["song_id"]
        previous = state["results"].get(sid, {})
        path = Path(previous.get("path", "")) if previous.get("path") else None
        if (path and path.is_file() and not path.is_symlink()
                and path.parent == directory
                and hashlib.sha256(path.read_bytes()).hexdigest() == previous.get("sha256")):
            continue
        try:
            reused = setlist.reused(state, sid)
            if reused:
                state["results"][sid] = {"title": song["title"], **reused}
                save()
                continue
            selected_key_id = key_by_song[sid]["key_id"]
            sources = inventory(api, dict(song, selected_key_id=selected_key_id))
            source = choose(sources, state["resource_reviews"].get(sid, {}), selected_key_id)
            if source is None:
                result = {"status": "missing", "message": "No Chinese lyrics or eligible numbered score; other-key scores are not substituted."}
            else:
                content = (source["text"].encode("utf-8") if "text" in source
                           else api.file(source["attachment_id"]))
                validate_file(content, source["filename"], source["kind"])
                path = save_original(directory, source["filename"], content)
                result = {
                    "status": "downloaded", "kind": source["kind"], "source_id": source["id"],
                    "path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
                    "message": "Original numbered score only; no transcription." if source["kind"] == SCORE
                    else "Original Chinese lyrics.",
                }
                if source["kind"] == SCORE:
                    result["score_key_status"] = (
                        "matched_key_attachment" if source.get("key_id") == selected_key_id else "unverified"
                    )
                    if not source.get("key_id"):
                        result["message"] += " Score key is not verified against selected MP3."
        except WorkflowError as error:
            result = {"status": error.code, "message": str(error)}
        except OSError:
            result = {"status": "file_error", "message": "Local file operation failed."}
        state["results"][sid] = {"title": song["title"], **result}
        state["stage"] = "partial"
        save()
        if result["status"] == "auth_error":
            break
    if not any(r.get("status") == "auth_error" for r in state["results"].values()):
        keys.deliver(state, api, directory, save)
    finished = all(state["results"].get(s["song_id"], {}).get("status") in (
        "downloaded", "missing", "reused"
    ) and state["results"].get(s["song_id"], {}).get("audio", {}).get("status") == "downloaded"
        for s in plan["songs"])
    state["stage"] = "completed" if finished else "partial"
    save()
    return state["results"]
