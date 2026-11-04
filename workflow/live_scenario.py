#!/usr/bin/env python3
"""Opt-in real PCO/SQLite/MP3/Scripture/CLI scenario with simulated human gates.

Never run this as a real service approval. It performs no PCO content writes.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request

import spoken

ROOT = Path(__file__).resolve().parent


def speaking_slots(context, songs):
    """Bind simulated speech to real stored occurrences without claiming live timing."""
    slots = spoken.default_slots()
    for slot in slots:
        if slot["placement"] != "interlude":
            continue
        material = context["songs"][slot["song_position"] - 1]
        known = [i for i in material.get("interludes", []) if i["bars"] is not None]
        if not known:
            continue
        interlude = known[-1]
        slot.update(route_index=interlude["route_index"],
                    material_fingerprint=material["material_fingerprint"])
        song = songs[slot["song_position"] - 1]
        if song.get("meter") == "4/4" and spoken.positive(song.get("bpm")):
            slot["window"] = {
                "bars": interlude["bars"], "beats_per_bar": 4, "bpm": song["bpm"],
                "reserve_seconds": 5, "tempo_basis": "reference_only",
                "basis": "AUTOMATION ONLY: stored bars and 4/4 quarter-note reference BPM; not live acceptance.",
            }
        else:
            slot["timing_unknown_reason"] = "实际音乐窗口与语速待排练"
    return slots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-simulated-human-gates", action="store_true", required=True)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    events = []
    report = {"mode": "real_external_services_simulated_human_gates",
              "human_listened": False, "human_approved": False,
              "model_used": "Codex-authored scenario content; no independent model API",
              "events": events, "status": "running"}
    state_path = None

    def save():
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def call(command, payload=None, extra=()):
        argv = [sys.executable, "-B", str(ROOT / "workflow.py")]
        if state_path:
            argv += ["--state", str(state_path)]
        argv += [command, *extra]
        result = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                                text=True, capture_output=True, timeout=600)
        value = json.loads(result.stdout)
        events.append({"command": command, "returncode": result.returncode,
                       "response_sha256": hashlib.sha256(result.stdout.encode()).hexdigest()})
        save()
        print(command + ": " + ("OK" if result.returncode == 0 else "FAILED"), flush=True)
        if result.returncode:
            raise RuntimeError(json.dumps(value, ensure_ascii=False))
        return value

    def simulated(message):
        return "AUTOMATION TEST ONLY; no real human approval/listening: " + message

    try:
        initial = call("new", extra=("--runs-dir", str(directory / "sessions"), "--test-run"))
        state_path = Path(initial["state_path"])
        report["state_path"] = str(state_path)
        call("doctor")
        requirements = {
            "theme": "未认识神的迷茫，认识神改变我的生命，最后坚定跟随神",
            "stages": ["呼求与信靠", "恩典带来生命改变", "委身并跟随"],
            "slots": ["slow", "slow", "slow"], "mood": "柔和、真诚",
            "preferences": [{"title": t, "mode": "preferred"} for t in ("我呼求", "让我", "如你")],
            "notes": "自动化测试场景，沿用用户既有主题与歌序；不是新的现场确认。",
        }
        call("requirements", requirements)
        for title, expected in (("我呼求", "reuse"), ("让我", "miss"), ("如你", "reuse")):
            value = call("rag-search", extra=(title,))
            assert value["status"] == expected, (title, value["status"])
        # These pins are inspected again against the live catalog, never invented.
        pins = [
            ("我呼求", "25177643", "29186203", "43810653", "157650138", "277d218e605d8afb188e5bb4"),
            ("让我", "26911580", "31308709", "47959295", "181643806", None),
            ("如祢", "28332671", "33047715", "51442204", "201736357", "f005597bb3031db0e259d883"),
        ]
        inspected = []
        for title, sid, aid, *_ in pins:
            catalog = call("catalog", extra=("--query", title))
            assert sid in {s["song_id"] for s in catalog["songs"]}
            response = call("inspect", extra=(sid,))
            assert not response["errors"], response["errors"]
            row = next(s for s in response["arrangements"] if s["arrangement_id"] == aid)
            inspected.append(row)
        reasons = [
            ("先承认自己需要拯救，以向神呼求和信靠祂的恩典回应迷茫。",
             "位于起点，从人的无助转向神；歌词本身不是完整叙述未信经历，不夸大主题命中。"),
            ("歌词从救赎和圣灵帮助走向真诚敬拜、服事、爱人和顺服，呈现生命改变。",
             "承接呼求所得的恩典，把认识神落实到生活回应。"),
            ("亲近神、活出丰盛生命，并在 Bridge 放下自己、敬拜跟随，表达持续委身。",
             "以更像基督及跟随作终点，而非只停在情绪安慰。"),
        ]
        songs = []
        for row, (reason, position_reason) in zip(inspected, reasons):
            songs.append({
                "song_id": row["song_id"], "arrangement_id": row["arrangement_id"],
                "reason": reason, "position_reason": position_reason,
                "theme_evidence": reason,
                "evidence_kind": "current_lyrics" if row.get("lyrics") else "historical_source",
                "tempo": "slow", "tempo_basis": "柔和敬拜场景判断；真实库内 BPM/拍号已读取，未进行音乐语义听辨。",
                "unknowns": ["原始编曲标签与中文录音/历史歌词版本须现场核对；未验证歌手实际舒适音域。"],
            })
        call("propose", {"recommended_id": "A", "proposals": [{
            "id": "A", "label": "呼求、更新、委身", "direction": "沿用既有三首，保留清楚的生命回应推进",
            "order_reason": "从呼求与信靠，到救赎带来的实际改变，最后放下自己坚定跟随。",
            "tradeoffs": "只给一套有依据的场景方案，不为凑三套替换；未信迷茫主要由话语说明，不说歌词逐字讲述。",
            "songs": songs,
        }]})
        state = call("show")
        call("confirm", {"proposal_id": "A", "revision": state["revision"], "confirmed": True,
                         "user_message": simulated("confirm order"), "accept_unknowns": True})
        for _, sid, _, _, _, rid in pins:
            if rid:
                call("material-prepare", {"song_id": sid, "id": rid,
                     "version_basis": "按用户指定历史歌单版本选择；与当前 PCO 中文歌名对应，编曲标签差异已披露。"})
        call("key-requirements", {
            "male_count": 1, "female_count": 1,
            "profiles": [{"id": "m", "gender": "male", "register": "middle", "count": 1},
                         {"id": "f", "gender": "female", "register": "middle", "count": 1}],
            "notes": "测试参数，不是用户实际歌手配置；原调仅作可重复的资源链路测试。",
        })
        options = call("key-options")
        report["key_options_checked"] = True
        call("key-propose", {"recommended_id": "K1", "proposals": [{
            "id": "K1", "reason": "采用本次核验过的原调 G/E/A 测试资源链路，不代表音域推荐已验收。",
            "songs": [{
                "song_id": sid, "key_id": kid, "attachment_id": attachment,
                "lead_groups": ["m", "f"], "reason": "真实现有 Key 的中文原调附件，版本明确。",
                "range_evidence": "实际旋律音域和歌手舒适音域均未测定，必须真人试唱。",
                "unknowns": ["不能根据低中高标签证明合适；本次没有真人试听。"],
            } for _, sid, _, kid, attachment, _ in pins],
        }]})
        previews = call("key-preview", {"proposal_id": "K1"})
        assert len(previews) == 3 and all(p["status"] == "prepared" for p in previews.values())
        state = call("show")
        call("key-confirm", {"proposal_id": "K1", "revision": state["keys"]["revision"],
                            "confirmed": True, "listened": True, "accept_unknowns": True,
                            "user_message": simulated("exercise post-audition gate, not an actual audition")})
        inventory = call("resources")
        assert inventory[pins[0][1]]["status"] == inventory[pins[2][1]]["status"] == "reused"
        results = call("download", extra=("--output", str(directory / "downloads")))
        assert all(r["audio"]["status"] == "downloaded" for r in results.values()), results
        assert [results[p[1]]["status"] for p in pins] == ["reused", "downloaded", "reused"]
        decoder = shutil.which("ffmpeg")
        assert decoder, "ffmpeg is required for the live full-file decoding check."
        for sid, result in results.items():
            audio = result["audio"]
            assert hashlib.sha256(Path(audio["path"]).read_bytes()).hexdigest() == previews[sid]["sha256"]
            subprocess.run([decoder, "-nostdin", "-v", "error", "-xerror",
                            "-protocol_whitelist", "file,pipe", "-i", audio["path"],
                            "-f", "null", "-"], capture_output=True, check=True, timeout=90)
        report["full_file_ffmpeg_decode_passed"] = 3
        lyric_path = Path(results[pins[1][1]]["path"])
        assert lyric_path.suffix.lower() == ".txt"
        raw = lyric_path.read_text()
        # Parse only explicit labels in the downloaded original, no inferred audio sections.
        sections, current = [], None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.fullmatch("Verse|Chorus", line, re.I):
                current = {"label": "V" if line.lower() == "verse" else "C", "lines": []}
                sections.append(current)
            elif re.fullmatch("Intro|Interlude", line, re.I):
                current = None
            elif current:
                current["lines"].append(line)
        assert {s["label"] for s in sections} == {"V", "C"}
        call("material-prepare", {
            "song_id": pins[1][1], "version_basis": "当前确认编曲的原始中文歌词字段",
            "structure_basis": "原文字段明确 Verse/Chorus；演唱顺序仅为现场提案，不冒充音频还原。",
            "lyric_sections": sections,
            "route": [{"label": x, "cue": ""} for x in ("IN", "V", "C", "IN", "V", "C", "END")],
        })

        verses = []
        for page, chapter, number, reference in (
            ("詩篇", 50, 15, "诗篇50:15"), ("羅馬書", 12, 1, "罗马书12:1"),
            ("哥林多後書", 3, 18, "哥林多后书3:18"),
        ):
            url = "https://zh.wikisource.org/w/api.php?" + urllib.parse.urlencode({
                "action": "parse", "page": "聖經 (和合本)/" + page, "prop": "wikitext", "format": "json",
            })
            request = urllib.request.Request(url, headers={"User-Agent": "RebornWorkflowScenario/1.0"})
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.load(response)
            text = data["parse"]["wikitext"]["*"]
            quote = re.search(r"\{\{verse\|" + str(chapter) + r"\|" + str(number) + r"\}\}([^\n]+)", text)
            assert quote
            verse = {"reference": reference, "translation": "和合本", "quote": quote.group(1),
                     "mode": "quotation", "source": url, "verification": "source_checked"}
            verses.append(verse)
            (directory / (page + "-source.json")).write_text(json.dumps(data, ensure_ascii=False))
        report["scripture_sources_fetched_live"] = 3
        timing_context = call("spoken-context")
        slots = speaking_slots(timing_context, inspected)
        call("spoken-requirements", {"slots": slots,
                                    "notes": "测试稿，参考窗口仅用于保守估计，不代表现场排练验收。"})
        bodies = [
            ("主啊，我们承认，有时不知道方向，也不知道怎样面对眼前的重担。",
             "谢谢祢让我们可以向祢呼求。求祢赦免我们只依靠自己的时候，使我们重新信靠祢。",
             "愿祢扶持我们，把今天的难处和往后的道路都交在祢手中。"),
            ("我们向神呼求，也因祂的怜悯得着拯救；祂的恩典不只安慰我们，也邀请我们回应。",
             verses[1]["quote"],
             "愿我们的赞美不只在口中，爱不只在言语；让顺服和真诚成为每天的生活。"),
            ("当我们愿意把生活交给神，生命的改变就不再只是一时的感动，而是持续被祂更新。",
             verses[2]["quote"],
             "愿我们越靠近祂，越活出祂的心意；放下自己的坚持，在每一步中坚定跟随。"),
            ("主啊，我们愿意亲近祢，也愿意让祢改变我们。",
             "不只在顺利时敬拜，也在看不清前路时信靠；求祢使我们的心不再退缩。",
             "我们把自己交给祢，求祢带领我们一生跟随。"),
        ]
        cues = []
        for i, slot in enumerate(("song1_interlude", "song2_before_intro", "song3_before_intro", "song3_interlude")):
            opening, body, closing = bodies[i]
            cues.append({
                "slot_id": slot, "opening": opening, "body": body, "closing": closing,
                "previous_connection": "承接呼求得恩典，再进入生命更新；承接更新，再进入持续委身。",
                "song_connection": reasons[min(i, 2)][0],
                "delivery_note": "留白后由主领示意继续；实际长度待排练。",
                "estimated_seconds": 20 if i == 3 else 40,
                "scriptures": [verses[i]] if i in (1, 2) else [],
                "unknowns": ["实际音乐窗口与语速待排练"],
            })
        call("spoken-propose", {"cues": cues})
        state = call("show")
        call("spoken-confirm", {"revision": state["spoken"]["revision"], "confirmed": True,
                                "accept_unknowns": True, "user_message": simulated("confirm authored spoken fixture")})
        call("spoken-export", extra=("--output-dir", str(directory / "spoken")))
        part = call("setlist-propose", {
            "main": [{"song_id": p[1], "scripture": verse} for p, verse in zip(pins, verses)],
            "response": {"song_id": pins[0][1]}, "ending": {"song_id": pins[2][1]},
        })
        # Negative acceptance: no formal output before its own confirmation.
        argv = [sys.executable, "-B", str(ROOT / "workflow.py"), "--state", str(state_path),
                "setlist-export", "--output", str(directory / "must-not-exist.docx")]
        rejected = subprocess.run(argv, capture_output=True, text=True)
        assert rejected.returncode == 2 and not (directory / "must-not-exist.docx").exists()
        report["unconfirmed_export_rejected"] = True
        call("setlist-confirm", {"revision": part["revision"], "confirmed": True,
                                 "user_message": simulated("confirm assembly selection")})
        exported = call("setlist-export", extra=("--output", str(args.output.resolve())))
        assert exported["status"] == "test_artifact"
        from docx import Document
        doc = Document(exported["path"])
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "非现场定稿" in text
        assert all(v["quote"] in text for v in verses)
        assert text.count("【祷告】") == 1 and text.count("【衔接话语】") == 3
        assert all("BPM=" in p.text and re.search(r"BPM=$", p.text) for p in doc.paragraphs if "BPM=" in p.text)
        sidecar = json.loads(Path(exported["sources"]).read_text())
        assert [s["key"] for s in sidecar["selection"]["main"]] == ["G", "E", "A"]
        ending = sidecar["sources"][-1]
        import setlist
        r = setlist.record(part["database"], ending["id"])
        assert [x["label"] for x in r["route"]] == ["IN", "V1", "V2", "C", "B"]
        report.update(status="passed", artifact=exported,
                      document_downloads=1, rag_reused_documents=2,
                      real_mp3s_decoded_and_delivered=3, ending_route=[x["label"] for x in r["route"]],
                      unknowns=["真人试听/试唱、实际音域、活动 BPM 和小节长度未验收",
                                "编曲标签与历史中文版本关系仍需现场核对",
                                "推荐/话语为 Codex 编写的场景输入，非独立模型自动生成测试"])
        save()
        print("PASS: real external resources, simulated human gates; " + str(directory / "report.json"))
    except Exception:
        report["status"] = "failed"
        # No raw HTTP responses, credentials or transient signed URLs.
        report["failure"] = "Scenario failed; inspect event boundary and local state."
        save()
        raise


if __name__ == "__main__":
    main()
