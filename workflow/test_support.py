"""Explicitly simulated key/MP3 data for isolated regression tests."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import keys

FAKE_MP3 = b"simulated mp3 bytes; codec validation is mocked in unit tests"
VOCALS = {
    "male_count": 1, "female_count": 1,
    "profiles": [
        {"id": "m", "gender": "male", "register": "middle", "count": 1},
        {"id": "f", "gender": "female", "register": "high", "count": 1},
    ],
}


def visual_fixture(page_count=4):
    """Simulated assertions only; never a claim that a native UI was inspected."""
    return {
        "app": "WPS", "page_count": page_count,
        "pages": [{"page": page, "checks": ["chinese", "clipping", "overlap", "pagination"],
                   "blocks": [f"main:{page}"] if page <= 3 else
                   ["response", "ending"] if page == 4 else ["spoken"],
                   "note": "AUTOMATION ONLY simulated page observation"}
                  for page in range(1, page_count + 1)],
        "songs": [{"position": position, "checks": [
            "heading_baseline", "first_lyric_baseline", "table_row_heights",
            "table_content_width", "instrument_notation", "fonts", "font_sizes",
            "section_colors", "paragraph_spacing", "lyric_spacing", "table_style",
            "table_dimensions", "single_song_per_page", "lyrics_pagination",
            "left_edges", "table_lyric_gap", "table_width_balance", "section_spacing"],
            "pages": [position], "lyrics_pages": [position],
            "note": "AUTOMATION ONLY simulated song observation"} for position in (1, 2, 3)],
        "response_ending": {
            "page": 4, "checks": ["fonts", "font_sizes", "section_colors", "paragraph_spacing",
                                 "table_style", "table_dimensions", "instrument_notation", "separate_page",
                                 "left_edges"],
            "note": "AUTOMATION ONLY simulated response/ending observation"},
    }


def catalog(sid):
    return [{
        "key_id": str(int(sid) + 100), "label": "G", "starting_key": "G", "minor": False,
        "ending_key": None, "ending_minor": False, "updated_at": "fixture", "audio_checked": True,
        "audios": [{
            "attachment_id": str(int(sid) + 1000), "filename": f"Song-{sid}-G.mp3",
            "downloadable": True, "mp3_allowed": True, "updated_at": "fixture", "size": len(FAKE_MP3),
        }],
    }]


def inventory(api, song):
    return catalog(song["song_id"])


def proposal():
    return {
        "recommended_id": "K1", "proposals": [{
            "id": "K1", "reason": "模拟推荐，实际需用户试听",
            "songs": [{
                "song_id": str(i), "key_id": str(i + 100), "attachment_id": str(i + 1000),
                "lead_groups": ["m", "f"], "reason": "模拟试唱方案",
                "range_evidence": "仅低中高分类，非实测音域", "unknowns": [],
            } for i in (1, 2, 3)],
        }],
    }


def approve(state, folder):
    keys.requirements(state, VOCALS)
    api = Mock()
    api.file.return_value = FAKE_MP3
    with patch("keys.inventory", side_effect=inventory), patch("keys.validate_mp3"):
        keys.refresh(state, api)
        keys.propose(state, proposal())
        keys.preview(state, {"proposal_id": "K1"}, api, Path(folder))
    keys.confirm(state, {
        "revision": state["keys"]["revision"], "proposal_id": "K1",
        "confirmed": True, "listened": True, "user_message": "模拟用户：已听完三首，确认这些调",
    })
    return state


def keyed(state):
    with tempfile.TemporaryDirectory() as folder:
        return approve(state, folder)
