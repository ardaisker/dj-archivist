#!/usr/bin/env python3
"""Offline tests for dj-archivist's core logic. Run: python3 tests/test_core.py

No network, no slskd, no ffmpeg: every path points into a temp dir (HOME included), and the slskd API is
replaced by a function that fails the test if anything tries to call it."""
import contextlib
import io
import os
import pathlib
import shutil
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET

TMP = pathlib.Path(tempfile.mkdtemp(prefix="dj-archivist-test-"))
os.environ["HOME"] = str(TMP / "home")                  # defaults can never reach a real ~/.dj-archivist
for var in ("DJ_ARCHIVIST_CONFIG", "SLSKD_URL", "SLSKD_API_KEY"):
    os.environ.pop(var, None)

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill" / "dj-archivist" / "scripts"))
import archivist as A  # noqa: E402


def _no_network(*args, **kwargs):
    raise AssertionError("network call attempted in an offline test")


A.api = _no_network
A.search = _no_network


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def fresh(**over):
    """A new temp workspace and a config that points only into it."""
    d = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    cfg = {"archive_dir": str(d / "archive"), "work_dir": str(d / "work"), "state_dir": str(d / "state"),
           "tracklist_csv": str(d / "tracklist.csv"), "inbox_file": str(d / "inbox.txt"),
           "local_import_dir": str(d / "downloads"),
           "playlist_tree": [["House", None], ["UK Garage", None], ["Sets", [["Warm-up", None]]]],
           "set_playlists": ["Warm-up"],
           "slskd": {"url": "http://127.0.0.1:9", "api_key": "offline-test", "yml_path": str(d / "slskd.yml"),
                     "binary": str(d / "slskd")}}
    cfg.update(over)
    A.configure(cfg)
    return d


def row(artist, title, length="", **kw):
    r = {c: "" for c in A.COLUMNS}
    r.update(artist=artist, title=title, length=length, **kw)
    return r


def cand(user, fn, size=0, length=None, br=None, vbr=None, free_slot=True, queue=0, speed=1_000_000):
    ext = pathlib.PurePosixPath(fn.replace("\\", "/")).suffix.lower()
    c = {"user": user, "fn": fn, "ext": ext, "size": size, "free_slot": free_slot, "queue": queue, "speed": speed,
         "last_seen": time.time()}
    for k, v in (("len", length), ("br", br), ("vbr", vbr)):
        if v is not None:
            c[k] = v
    return c


def ts_with(*cands):
    return {"candidates": {c["user"] + "|" + c["fn"]: c for c in cands}, "tried": {}}


def users_of(keys):
    return [k.split("|")[0] for k in keys]


# ---------------------------------------------------------------- identity matching
def test_identity_matching():
    fresh()
    r = row("Nova Tide", "Golden Hours (Zephyr Extended Remix)")
    assert A.matches(r, "Music\\Nova Tide\\Nova Tide - Golden Hours (Zephyr Extended Remix).mp3")
    # artist words may sit in a folder; core title and remixer words must be in the file name
    assert A.matches(r, "Music/Nova Tide/Singles/01 Golden Hours (Zephyr Remix).flac")
    assert not A.matches(r, "Music/Nova Tide/01 Golden Hours (Original Mix).mp3")        # remixer missing
    assert not A.matches(r, "Music/Other Band - Golden Hours (Zephyr Remix).mp3")         # artist missing
    assert not A.matches(r, "Music/Nova Tide/Zephyr Remix/Golden Hours.mp3")              # remixer only in a folder
    # somebody else's remix of the same song is rejected
    r2 = row("Nova Tide", "Golden Hours (Extended Mix)")
    assert A.matches(r2, "Nova Tide - Golden Hours (Extended Mix).mp3")
    assert not A.matches(r2, "Nova Tide - Golden Hours (Quillon Extended Mix).mp3")
    assert A.other_remix(r2, "Nova Tide - Golden Hours (Quillon Extended Mix).mp3")
    assert not A.other_remix(r2, "Nova Tide - Golden Hours (Extended Club Mix).mp3")     # neutral words only
    assert not A.other_remix(r2, "Nova Tide - Golden Hours (2019 Remaster).mp3")          # not a remix bracket
    # "Original Artist - Song" titles: the original artist counts as an artist, the remixer is required
    r3 = row("Qelvin", "Maroon Coast - Silver Line (Qelvin Edit)")
    assert A.matches(r3, "Maroon Coast - Silver Line (Qelvin Edit).wav")
    assert A.matches(r3, "Qelvin/Silver Line (Qelvin Edit).aiff")
    assert not A.matches(r3, "Maroon Coast - Silver Line.mp3")
    # dotted abbreviations and apostrophes
    assert A.norm("W.T.F. - Lose Control") == "wtf lose control"
    assert A.norm("Don't Stop") == "dont stop"
    assert A.matches(row("W.T.F.", "Night Drive"), "WTF - Night Drive.mp3")
    assert A.matches(row("WTF", "Night Drive"), "W.T.F. - Night Drive (Extended Mix).mp3")
    assert not A.matches(row("WTF", "Night Drive"), "WTF - Night Driver.mp3")
    # clean/dirty preference
    rc = row("Nova Tide", "Golden Hours (Clean)")
    assert A.clean_dirty_mismatch(rc, "Nova Tide - Golden Hours (Dirty).mp3") == 1
    assert A.clean_dirty_mismatch(rc, "Nova Tide - Golden Hours (Explicit).mp3") == 1
    assert A.clean_dirty_mismatch(rc, "Nova Tide - Golden Hours (Clean).mp3") == 0
    assert A.clean_dirty_mismatch(row("Nova Tide", "Golden Hours (Dirty)"), "Golden Hours (Clean).mp3") == 1


# ---------------------------------------------------------------- length evidence
def test_size_based_length_evidence():
    fresh()
    assert A.tol(200) == 3.0 and A.tol(600) == 6.0
    assert A.parse_length("06:31") == 391.5 and A.parse_length("6:31") == 391.5
    assert A.parse_length("391") == 391.0 and A.parse_length("") is None and A.parse_length("1:02:03") == 3723.5

    def mp3(size):
        return {"ext": ".mp3", "size": size}
    assert A.fit(mp3(40000 * 300 + 1_000_000), 300) == "size"      # 320 kbps audio + 1 MB of tags
    assert A.fit(mp3(40000 * 300 + 3_900_000), 300) == "size"      # up to 4 MB of tags and cover art
    assert A.fit(mp3(40000 * 200), 300) is None                    # too small: a shorter version
    assert A.fit(mp3(40000 * 420), 300) is None                    # too big: a longer version
    assert A.fit({"ext": ".wav", "size": 176400 * 300}, 300) == "size"     # 16 bit / 44.1 kHz
    assert A.fit({"ext": ".aiff", "size": 264600 * 300}, 300) == "size"    # 24 bit / 44.1 kHz
    assert A.fit({"ext": ".wav", "size": 176400 * 250}, 300) is None
    assert A.fit({"ext": ".flac", "size": 30_000_000}, 300) == "weak"      # FLAC: only a coarse band
    assert A.fit({"ext": ".flac", "size": 5_000_000}, 300) is None
    assert A.fit({"ext": ".mp3", "size": 0, "len": 302}, 300) == "exact"   # length reported by the peer
    assert A.fit({"ext": ".mp3", "size": 0, "len": 310}, 300) is None
    assert A.cluster_length({"ext": ".wav", "size": 176400 * 300}) == 300.0
    assert A.cluster_length({"ext": ".mp3", "size": 12_000_000}) is None   # MP3 size is not exact enough


# ---------------------------------------------------------------- clustering and the target
def test_clustering_and_target_choice():
    fresh()
    ext_a = cand("u1", "Nova Tide - Golden Hours (Extended Mix).mp3", length=361)
    ext_b = cand("u2", "Nova Tide - Golden Hours (Extended Mix).flac", length=359)
    ext_c = cand("u3", "Golden Hours (Extended Mix).wav", size=int(176400 * 360.4))   # length from the PCM size
    radio = cand("u4", "Nova Tide - Golden Hours (Radio Edit).mp3", length=212)
    cl = A.clusters([ext_a, ext_b, ext_c, radio])
    assert len(cl) == 2 and cl[0]["sources"] == 3 and abs(cl[0]["center"] - 360.4) < 1.5
    assert "extended" in cl[0]["labels"] and "radio" in cl[1]["labels"]
    # no reference length: the confident cluster wins (the single radio source does not count)
    r = row("Nova Tide", "Golden Hours")
    target, source, _ = A.find_target(r, ts_with(ext_a, ext_b, ext_c, radio))
    assert source == "cluster" and abs(target - 360.4) < 1.5
    # the reference length beats the clusters, a decision beats the reference
    r_ref = row("Nova Tide", "Golden Hours", length="4:02")
    assert A.find_target(r_ref, ts_with(ext_a, ext_b))[:2] == (242.5, "reference")
    ts = ts_with(ext_a, ext_b)
    ts.update(target=300.0, target_source="decision")
    assert A.find_target(r_ref, ts)[:2] == (300.0, "decision")
    # two labelled clusters with 2+ sources each: undecided
    club_a = cand("u5", "Nova Tide - Golden Hours (Club Mix).mp3", length=421)
    club_b = cand("u6", "Nova Tide - Golden Hours (Club Mix).mp3", length=420)
    target, source, desc = A.find_target(r, ts_with(ext_a, ext_b, club_a, club_b))
    assert target is None and source is None and "2 confident clusters" in desc
    # ... unless the title names the version
    r_ext = row("Nova Tide", "Golden Hours (Extended Mix)")
    target, source, _ = A.find_target(r_ext, ts_with(ext_a, ext_b, club_a, club_b))
    assert source == "cluster" and abs(target - 360) < 2
    # a single source is not enough; a radio cluster counts only when the title says radio
    assert A.find_target(r, ts_with(ext_a))[0] is None
    radio_b = cand("u9", "Nova Tide - Golden Hours (Radio Edit).mp3", length=211)
    assert A.find_target(r, ts_with(radio, radio_b))[0] is None
    assert A.find_target(row("Nova Tide", "Golden Hours (Radio Edit)"), ts_with(radio, radio_b))[1] == "cluster"
    # inbox items need an explicit extended/original/club label; tracklist rows also accept dub/mix/edit
    dub_a = cand("u7", "Nova Tide - Golden Hours (Dub).mp3", length=390)
    dub_b = cand("u8", "Nova Tide - Golden Hours (Dub).mp3", length=391)
    assert A.find_target(r, ts_with(dub_a, dub_b))[1] == "cluster"
    r_inbox = row("Nova Tide", "Golden Hours", inbox_line="Nova Tide - Golden Hours")
    assert A.find_target(r_inbox, ts_with(dub_a, dub_b))[0] is None
    assert A.find_target(r_inbox, ts_with(ext_a, ext_b))[1] == "cluster"
    # same user twice is still one source
    assert A.find_target(r, ts_with(ext_a, cand("u1", "Golden Hours (Extended Mix).mp3", length=360)))[0] is None


# ---------------------------------------------------------------- ranking
def test_ranking():
    fresh()
    now = time.time()
    r = row("Nova Tide", "Golden Hours", length="6:00")
    target = A.parse_length("6:00")
    mp3_far = cand("far", "Nova Tide - Golden Hours.mp3", length=360, br=320, free_slot=False, queue=5000)
    flac_near = cand("near", "Nova Tide - Golden Hours.flac", length=360)
    mp3_near = cand("near2", "Nova Tide - Golden Hours.mp3", length=361, br=320, free_slot=False, queue=3)

    def ranked(st, *cands, row_=r):
        return users_of(A.ranked_candidates(st, row_, ts_with(*cands), target, now))
    # reachability first (an MP3 320 behind a 5000-person queue loses even to FLAC), then the format priority
    assert ranked({"users": {}}, mp3_far, flac_near, mp3_near) == ["near2", "near", "far"]
    # excluded: wrong length, VBR, sub-320 MP3, banned user, user on a quota pause, user with 3 recent rejections
    wrong_len = cand("w", "Nova Tide - Golden Hours.mp3", length=300, br=320)
    vbr = cand("v", "Nova Tide - Golden Hours.mp3", length=360, br=320, vbr=True)
    low = cand("l", "Nova Tide - Golden Hours.mp3", length=360, br=256)
    banned = cand("b", "Nova Tide - Golden Hours.mp3", length=360, br=320)
    quota = cand("q", "Nova Tide - Golden Hours.mp3", length=360, br=320)
    rejecter = cand("r", "Nova Tide - Golden Hours.mp3", length=360, br=320)
    small_m4a = cand("m", "Nova Tide - Golden Hours.m4a", length=360, br=192)
    st = {"users": {"b": {"banned": True}, "q": {"skip_until": now + 3600},
                    "r": {"rejects": 3, "last_reject": now - 3600}}}
    assert ranked(st, mp3_near, wrong_len, vbr, low, banned, quota, rejecter, small_m4a) == ["near2"]
    st["users"]["r"]["last_reject"] = now - 8 * 24 * 3600          # rejections older than 7 days
    st["users"]["q"]["skip_until"] = now - 1                          # quota pause over
    assert sorted(ranked(st, rejecter, quota)) == ["q", "r"]
    # a failed candidate waits 24 h; a permanent failure never comes back
    ts = ts_with(mp3_near)
    key = next(iter(ts["candidates"]))
    ts["tried"][key] = {"permanent": False, "retry_after": now + 100}
    assert A.ranked_candidates({"users": {}}, r, ts, target, now) == []
    ts["tried"][key] = {"permanent": False, "retry_after": now - 1}
    assert A.ranked_candidates({"users": {}}, r, ts, target, now) == [key]
    ts["tried"][key] = {"permanent": True, "retry_after": now - 1}
    assert A.ranked_candidates({"users": {}}, r, ts, target, now) == []
    # clean/dirty ranks before speed
    r_clean = row("Nova Tide", "Golden Hours (Clean)", length="6:00")
    dirty = cand("d", "Nova Tide - Golden Hours (Dirty).mp3", length=360, br=320, speed=9_999_999)
    clean = cand("c", "Nova Tide - Golden Hours (Clean).mp3", length=360, br=320, speed=1)
    assert ranked({"users": {}}, dirty, clean, row_=r_clean) == ["c", "d"]
    # length evidence: a reported length beats one computed from the size
    by_size = cand("s", "Nova Tide - Golden Hours.mp3", size=40000 * 360 + 1_000_000, speed=9_999_999)
    exact = cand("e", "Nova Tide - Golden Hours.mp3", length=360, br=320, speed=1)
    assert ranked({"users": {}}, by_size, exact) == ["e", "s"]
    # format_priority comes from the config: here FLAC first, WAV refused
    fresh(format_priority=["flac", "mp3_320"])
    wav = cand("wv", "Nova Tide - Golden Hours.wav", size=int(176400 * 360.5))
    assert ranked({"users": {}}, mp3_near, flac_near, wav) == ["near", "near2"]


def test_failures_update_users():
    fresh()
    now = time.time()
    st = {"users": {}}
    r = row("Nova Tide", "Golden Hours", id="1", status="downloading")
    ts = {"active": {"key": "x|a.mp3", "user": "x"}, "tried": {}, "history": []}
    with quiet():
        A.fail(st, r, ts, "Completed, Rejected Banned", now=now)
    assert st["users"]["x"]["banned"] and st["users"]["x"]["rejects"] == 1
    assert r["status"] == "candidate" and ts["active"] is None and ts["tried"]["x|a.mp3"]["permanent"]
    ts["active"] = {"key": "y|b.mp3", "user": "y"}
    with quiet():
        A.fail(st, r, ts, "Completed, Rejected Too many megabytes", now=now)
    assert st["users"]["y"]["skip_until"] >= now + 24 * 3600 and not st["users"]["y"].get("banned")
    ts["active"] = {"key": "z|c.mp3", "user": "z"}
    with quiet():
        A.fail(st, r, ts, "download stalled", permanent=False, now=now)
    assert st["users"]["z"]["rejects"] == 0 and ts["tried"]["z|c.mp3"]["retry_after"] == now + 24 * 3600


# ---------------------------------------------------------------- query generation
def test_keyword_query_generation():
    fresh()
    r = row("Nova Tide", "Golden Hours (Zephyr Remix)")
    names = A.name_queries(r)
    assert names == ["nova tide golden hours zephyr", "nova tide golden hours", "zephyr golden hours", "golden hours"]
    kw = A.keyword_queries(r)
    assert kw[:6] == ["zephyr golden", "zephyr hours", "nova golden", "nova hours", "tide golden", "tide hours"]
    assert "zephyr golden hours" in kw and kw[-3:] == ["zephyr", "nova", "tide"]
    # every round: the name variants, then a rotating slice of keyword_queries_per_round (8) keyword queries
    rest = [q for q in kw if q not in names]
    q0, q1 = A.queries_for_round(r, 0), A.queries_for_round(r, 1)
    assert q0 == names + rest[:8] and q1 == names + (rest[8:] + rest[:8])[:8]
    # owner first: the row's artist and remixer outrank the original artist of an "X - Song" title
    r2 = row("Qelvin", "Maroon Coast - Silver Line (Qelvin Edit)")
    kw2 = A.keyword_queries(r2)
    assert kw2[0] == "qelvin silver" and kw2.index("qelvin silver") < kw2.index("maroon silver")
    # common words never stand alone or only with other common words; two-letter words stay out
    r3 = row("Kai Lumen", "Love On My House Party")
    kw3 = A.keyword_queries(r3)
    assert kw3 and "lumen love" in kw3 and "lumen" in kw3
    for q in kw3:
        ws = q.split()
        assert not all(w in A.COMMON_WORDS for w in ws), q
        assert all(len(w) > 2 for w in ws), q
    # a word with digits is distinctive (a bare number is dropped: it looks like a track number)
    kw4 = A.keyword_queries(row("Rho", "Track TB303 Sunrise 02"))
    assert "tb303" in kw4 and not any("02" in q.split() for q in kw4)


# ---------------------------------------------------------------- rounds and decisions
def test_rounds_and_decisions():
    fresh()
    now = time.time()
    st = {"users": {}, "tracks": {}}
    r = row("Nova Tide", "Golden Hours", id="7")
    ts = A.track_state(st, "7")
    ts["candidates"] = ts_with(cand("u1", "Nova Tide - Golden Hours (Extended Mix).mp3", length=361),
                               cand("u2", "Nova Tide - Golden Hours (Extended Mix).mp3", length=360),
                               cand("u3", "Nova Tide - Golden Hours (Club Mix).mp3", length=421),
                               cand("u4", "Nova Tide - Golden Hours (Club Mix).mp3", length=420))["candidates"]
    with quiet():
        A.evaluate(st, r, ts, now, last_query=False)             # more query variants left in this round
        assert r["status"] == "waiting" and ts["query_i"] == 1 and ts["next_search"] == now + 300
        A.evaluate(st, r, ts, now, last_query=True)              # round 1 over: back off 2 h
        assert r["status"] == "waiting" and ts["round"] == 1 and ts["next_search"] == now + 7200
        A.evaluate(st, r, ts, now, last_query=True)
        assert ts["round"] == 2 and ts["next_search"] == now + 28800
        A.evaluate(st, r, ts, now, last_query=True)              # third round: ask for a decision
        assert r["status"] == "decision" and "target unclear" in r["check"] and "6:01 (2 sources)" in r["check"]
    # decide through the CLI function: the tracklist and the state are files in the temp workspace
    A.write_rows([r])
    A.save_state(st)
    with quiet():
        A.cmd_decide("7", "6:01")
    r2 = A.read_rows()[0]
    ts2 = A.load_state()["tracks"]["7"]
    assert r2["status"] == "candidate" and r2["check"] == "decision: 6:01" and ts2["target_source"] == "decision"
    # a decided target survives the next evaluation, and the decision note stays
    with quiet():
        A.evaluate(A.load_state(), r2, ts2, now, last_query=True)
    assert r2["status"] == "candidate" and r2["check"] == "decision: 6:01"
    with quiet():
        A.cmd_decide("7", "wait")
    assert A.read_rows()[0]["status"] == "waiting" and A.load_state()["tracks"]["7"]["keep_waiting"]
    # after the last round: not_found
    ts3 = A.track_state({"tracks": {}}, "8")
    ts3["round"] = 5
    r3 = row("Kai Lumen", "Love Party", id="8")
    with quiet():
        A.round_over(r3, ts3, now, "no consistent length")
    assert r3["status"] == "not_found" and ts3["round"] == 6


# ---------------------------------------------------------------- inbox
def _archive_file(rel):
    p = A.C.archive / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not really audio")
    return rel


def test_inbox_parse_and_mark():
    fresh()
    raw = (b"# Heard at a warehouse party\r\n"
           b"- Nova Tide - Golden Hours   \r\n"
           b"* Kai Lumen - Love Party\r\n"
           b"\r\n"
           b"UK Garage:\r\n"
           b"1. Qelvin - Silver Line - DOWNLOADED\r\n"
           b"2) Maroon Coast - Night Drive\r\n"
           b"Caf\xe9 Society - Late Set\r\n"                    # not valid UTF-8: must survive byte for byte
           b"W.T.F. - Night Drive")                               # last line without a line ending
    inbox = A.C.inbox
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_bytes(raw)
    items = A.read_inbox()
    assert [i["line"] for i in items] == [2, 3, 6, 7, 8, 9]
    assert items[0] == {"line": 2, "text": "Nova Tide - Golden Hours", "heading": "Heard at a warehouse party",
                        "marked": False}
    assert items[1]["text"] == "Kai Lumen - Love Party"
    assert items[2] == {"line": 6, "text": "Qelvin - Silver Line", "heading": "UK Garage", "marked": True}
    assert items[3]["text"] == "Maroon Coast - Night Drive" and items[5]["heading"] == "UK Garage"
    A.write_rows([
        row("Nova Tide", "Golden Hours", id="1", status="done", playlists="House",
            file=_archive_file("House/Nova Tide - Golden Hours.mp3"), inbox_line="- Nova Tide - Golden Hours"),
        row("Kai Lumen", "Love Party", id="2", status="waiting", inbox_line="Kai Lumen - Love Party"),
        row("W.T.F.", "Night Drive", id="3", status="owned", playlists="UK Garage",
            file=_archive_file("UK Garage/W.T.F. - Night Drive.mp3"), inbox_line="W.T.F. - Night Drive"),
        row("Maroon Coast", "Night Drive", id="4", status="done", file="House/missing.mp3",
            inbox_line="Maroon Coast - Night Drive"),
    ])
    expected = (raw.replace(b"- Nova Tide - Golden Hours   \r\n", b"- Nova Tide - Golden Hours - DOWNLOADED\r\n")
                .replace(b"\r\nW.T.F. - Night Drive", b"\r\nW.T.F. - Night Drive - DOWNLOADED"))
    with quiet():
        assert A.mark_inbox() == 2
    assert inbox.read_bytes() == expected
    with quiet():
        assert A.mark_inbox() == 0                               # idempotent
    assert inbox.read_bytes() == expected
    # the file changes between our read and our write (the user saves it): nothing is written
    inbox.write_bytes(raw)
    edited = raw + b"\r\nNew Artist - New Track\r\n"
    original_mark_lines = A.mark_lines

    def user_saves_meanwhile(data, done):
        out = original_mark_lines(data, done)
        inbox.write_bytes(edited)
        return out
    A.mark_lines = user_saves_meanwhile
    try:
        with quiet():
            assert A.mark_inbox() == 0
    finally:
        A.mark_lines = original_mark_lines
    assert inbox.read_bytes() == edited
    with quiet():
        assert A.mark_inbox() == 2                               # the next pass marks it, keeping the new line
    assert inbox.read_bytes() == expected + b"\r\nNew Artist - New Track\r\n"
    # the listing shows the state of every item
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_inbox()
    listing = out.getvalue()
    assert "Kai Lumen - Love Party | [2] waiting" in listing and "Society - Late Set | NEW" in listing
    assert listing.count("DOWNLOADED") == 3


def test_add_from_inbox():
    fresh()
    A.write_rows([row("Nova Tide", "Golden Hours", id="1", playlists="House")])
    with quiet():
        A.cmd_add("Rho - Sunrise (heard at the club)", "Rho", "Sunrise (Extended Mix)", "6:5", "UK Garage")
        A.cmd_add("Rho - Sunrise (heard at the club)", "Rho", "Sunrise (Extended Mix)", "6:05", "UK Garage")
        A.cmd_add("nova tide - golden hours", "Nova Tide", "Golden Hours", "", "Warm-up", notes="set opener")
    rows = A.read_rows()
    assert len(rows) == 2                                        # the repeated item and the known track add no row
    new = rows[1]
    assert (new["id"], new["length"], new["folder"], new["source"], new["playlists"]) == \
        ("2", "06:05", "UK Garage", "inbox", "UK Garage")
    assert rows[0]["inbox_line"] == "nova tide - golden hours" and rows[0]["playlists"] == "House; Warm-up"
    assert rows[0]["folder"] == "House"                           # a set membership does not move the file
    assert A.playlists_of(rows[0]) == ["House", "Warm-up"] and A.primary_folder(rows[0]) == "House"
    assert A.primary_folder(row("X", "Y", playlists="Warm-up")) == "Sets/Warm-up"
    assert A.primary_folder(row("X", "Y", playlists="Unknown")) == "_other"


# ---------------------------------------------------------------- files and outputs
def test_csv_keeps_unknown_columns():
    fresh()
    A.C.csv.parent.mkdir(parents=True, exist_ok=True)
    A.C.csv.write_text("id,artist,title,status,my_rating\n1,Nova Tide,Golden Hours,pending,5\n"
                       "2,Kai Lumen,Love Party,done,3,STRAY CELL\n", encoding="utf-8")
    rows = A.read_rows()
    assert rows[0]["status"] == "" and rows[0]["my_rating"] == "5" and None not in rows[1]
    rows[0]["status"] = "waiting"
    A.write_rows(rows)
    text = A.C.csv.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(A.COLUMNS + ["my_rating"])
    again = A.read_rows()
    assert again[0]["status"] == "waiting" and again[0]["my_rating"] == "5" and again[1]["my_rating"] == "3"
    assert not list(A.C.csv.parent.glob("*.tmp"))


def test_cleanup_keeps_data():
    fresh()
    c = A.C
    (c.incomplete / "sub").mkdir(parents=True)
    old = time.time() - 7200
    placeholder = c.incomplete / "sub" / "a.mp3"
    placeholder.write_bytes(b"")
    os.utime(placeholder, (old, old))
    fresh_placeholder = c.incomplete / "b.mp3"
    fresh_placeholder.write_bytes(b"")
    partial = c.incomplete / "c.mp3"
    partial.write_bytes(b"x" * 10)
    os.utime(partial, (old, old))
    (c.incoming / "Remote Folder" / "Deeper").mkdir(parents=True)
    (c.incoming / "Has File").mkdir()
    (c.incoming / "Has File" / "t.mp3").write_bytes(b"x")
    c.rejected.mkdir(parents=True)
    (c.rejected / "1-bad.mp3").write_bytes(b"x")
    A.cleanup_placeholders()
    assert not placeholder.exists() and fresh_placeholder.exists() and partial.exists()
    assert not (c.incoming / "Remote Folder").exists() and (c.incoming / "Has File" / "t.mp3").exists()
    assert c.incomplete.is_dir() and (c.rejected / "1-bad.mp3").exists()
    # a verified download is found where slskd puts it, never in incomplete/
    (c.incoming / "Remote Folder").mkdir()
    (c.incoming / "Remote Folder" / "Track [Extended].mp3").write_bytes(b"x")
    (c.incomplete / "Other [Extended].mp3").write_bytes(b"x")
    assert A.downloaded_path({"filename": "@@u\\Music\\Remote Folder\\Track [Extended].mp3"}) == \
        c.incoming / "Remote Folder" / "Track [Extended].mp3"
    assert A.downloaded_path({"filename": "@@u\\Elsewhere\\Other [Extended].mp3"}) is None


def test_rekordbox_export():
    fresh()
    A.write_rows([
        row("Nova Tide", "Golden Hours", "06:00", id="1", status="done", playlists="House; Warm-up",
            file=_archive_file("House/Nova Tide - Golden Hours.mp3")),
        row("Nova Tide", "Golden Hours", id="2", status="duplicate", playlists="UK Garage",
            check="duplicate:1 (same track, two playlists)"),
        row("Kai Lumen", "Love Party", "05:30", id="3", status="owned", playlists="UK Garage",
            file=_archive_file("UK Garage/Kai Lumen - Love Party.aiff")),
        row("Rho", "Sunrise", id="4", status="waiting", playlists="House"),
    ])
    with quiet():
        A.export_rekordbox()
    root = ET.parse(A.C.archive / "rekordbox.xml").getroot()
    tracks = root.find("COLLECTION").findall("TRACK")
    assert root.find("COLLECTION").get("Entries") == "2" and len(tracks) == 2
    assert tracks[1].get("Kind") == "AIFF File" and tracks[0].get("Location").startswith("file://localhost/")
    nodes = {n.get("Name"): n for n in root.iter("NODE")}
    assert [t.get("Key") for t in nodes["House"].findall("TRACK")] == ["1"]
    assert [t.get("Key") for t in nodes["Warm-up"].findall("TRACK")] == ["1"]
    assert [t.get("Key") for t in nodes["UK Garage"].findall("TRACK")] == ["1", "2"]   # the duplicate maps to file 1
    assert nodes["Sets"].get("Type") == "0" and nodes["ROOT"].get("Count") == "3"
    index = (A.C.archive / "archive-index.csv").read_text(encoding="utf-8").splitlines()
    assert len(index) == 3 and index[0].startswith("folder,file,artist,title,length")
    readme = (A.C.archive / "README.md").read_text(encoding="utf-8")
    assert "| House | 1 | 0 h 6 min |" in readme and "1 more on the way" in readme


# ---------------------------------------------------------------- runner
def main():
    tests = [(name, fn) for name, fn in list(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
