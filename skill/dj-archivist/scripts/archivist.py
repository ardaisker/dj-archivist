#!/usr/bin/env python3
"""dj-archivist: rebuild a DJ archive from a tracklist through slskd (Soulseek), one verified file per track.

Flow: tracklist CSV -> paced slskd searches -> version choice by length -> download tracking -> ffprobe
verification -> tagging -> <archive>/<playlist folder>/Artist - Title.ext -> rekordbox.xml, index, README.md.

Rules in short:
- The right version is decided by LENGTH, not by its name: an explicit decision, else the reference length in
  the tracklist, else a length cluster seen at 2+ sources that carries a mix/edit label. Unclear cases are never
  picked silently: the track goes to status "decision" (see `decisions` and `decide`).
- Format priority (configurable): MP3 320 CBR > AIFF > FLAC > WAV > M4A >= 256 kbps. VBR or sub-320 MP3 is refused.
- Slow and steady: one search a minute, at most 2 transfers at once (1 per user); a source that refuses, queues
  too long or stalls is replaced by the next candidate; tracks not found are searched again with growing gaps.

Commands (slskd must be running: python3 slskd_setup.py --start):
  start / stop                 start the loop as a detached background process / stop it
  run                          the loop itself, in the foreground (start runs this in the background)
  round [--limit N]            search and download the first N open tracks, exit when they are settled (trial)
  status                       status counts, active downloads, recent events
  decisions                    tracks waiting for a decision, with the evidence (length clusters)
  decide ID mm:ss|wait|skip    decision: target length, keep waiting for the reference length, or skip the track
  inbox                        inbox items and their state (NEW = not imported yet)
  add --line ... --artist ... --title ... [--length mm:ss] --playlist ...   import a resolved inbox item
  mark                         append the inbox marker to lines whose tracks are archived (the loop does it too)
  search "query" [--artist --title --length]   one paced search that prints length clusters
  import-local [--dry-run] [--id ...]   file owned tracks from the local import dir and orphans from incoming/
  rekordbox                    write rekordbox.xml, archive-index.csv and README.md into the archive
  retag                        retag archive files whose artist/title differ from the tracklist or carry spam
Status values: pending (empty), waiting, candidate, downloading, done, owned, duplicate, decision, not_found.
Config: --config PATH, else $DJ_ARCHIVIST_CONFIG, else ~/.dj-archivist/config.json (defaults if missing).
Stdlib only, plus the ffmpeg and ffprobe binaries.
"""
import argparse
import contextlib
import csv
import fcntl
import io
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CONFIG = "~/.dj-archivist/config.json"
FORMAT_KEYS = ("mp3_320", "aiff", "flac", "wav", "m4a_256")

DEFAULTS = {
    "archive_dir": "~/Music/dj-archivist/archive",
    "work_dir": "~/Music/dj-archivist/work",          # slskd downloads: incoming/, incoming/incomplete; rejected/
    "state_dir": "~/.dj-archivist",
    "tracklist_csv": "~/.dj-archivist/tracklist.csv",
    "inbox_file": "~/.dj-archivist/inbox.txt",
    "local_import_dir": "~/Downloads",
    # Playlist tree for rekordbox.xml: a leaf is a playlist ("Name" or ["Name", null]), a folder is
    # ["Name", [children]]. Each playlist is also an archive folder (its tree path unless playlist_folders says so).
    "playlist_tree": [["House", None], ["Tech House", None], ["UK Garage", None], ["Sets", [["Warm-up", None]]]],
    "playlist_folders": {},
    "set_playlists": ["Warm-up"],
    "slskd": {"url": "http://localhost:5030", "api_key": "", "yml_path": "~/.dj-archivist/slskd.yml",
              "binary": "~/Applications/slskd/slskd"},
    "ffmpeg_dir": "",                                  # empty: ffmpeg/ffprobe from PATH
    "pacing": {
        "search_interval": 60,           # s between two searches (Soulseek throttles after ~20 quick searches)
        "loop": 15,                      # s per main loop iteration
        "max_active": 2,                 # transfers really downloading at once (remote-queued ones do not count)
        "max_open_requests": 6,          # all open requests, remote-queued ones included
        "enqueue_interval": 30,          # s between two download starts
        "remote_queue_timeout": 7200,    # s in a remote queue before that source is dropped (retry after 24 h)
        "start_timeout": 900,            # s stuck in Requested / Queued, Locally / Initializing
        "stall_timeout": 1200,           # s InProgress without new bytes
        "retry_backoff": [0, 7200, 28800, 86400, 259200, 604800],   # gap before each new search round
        "decision_after_rounds": 3,      # evidence but no clear target: ask for a decision from this round on
        "next_variant_delay": 300,       # s before the next query variant of the same round (at the earliest)
        "keyword_queries_per_round": 8,  # keyword-combination queries per round, after the name variants
        "reachable_queue": 50,           # no free slot and a longer queue: practically unreachable
    },
    "format_priority": list(FORMAT_KEYS),
    "spam_tag_patterns": [r"https?://", r"www\.", r"\.(com|net|org|ru|to|club)\b"],
    "inbox_marker": " - DOWNLOADED",
}


def _path(p, base):
    p = pathlib.Path(os.path.expandvars(str(p))).expanduser()
    return p if p.is_absolute() else base / p


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def _warn_unknown(data):
    for k in data:
        if k not in DEFAULTS and not k.startswith("_"):
            print(f"warning: unknown config key '{k}'", file=sys.stderr)
    for k in data.get("pacing") or {}:
        if k not in DEFAULTS["pacing"] and not k.startswith("_"):
            print(f"warning: unknown pacing key '{k}'", file=sys.stderr)
    for f in data.get("format_priority") or []:
        if f not in FORMAT_KEYS:
            print(f"warning: unknown format '{f}' (known: {', '.join(FORMAT_KEYS)})", file=sys.stderr)


def _parse_tree(nodes):
    """Config tree -> [(name, children or None)]. Accepts "Name", ["Name", null], ["Name", [...]] and
    {"name": ..., "children": [...]}."""
    out = []
    for n in nodes or []:
        if isinstance(n, str):
            out.append((n, None))
        elif isinstance(n, dict):
            ch = n.get("children")
            out.append((n["name"], _parse_tree(ch) if ch is not None else None))
        else:
            name, ch = (list(n) + [None])[:2]
            out.append((name, _parse_tree(ch) if ch is not None else None))
    return out


def _collect_folders(tree, prefix, out):
    for name, children in tree:
        if children is None:
            out.setdefault(name, "/".join(prefix + [name]))
        else:
            _collect_folders(children, prefix + [name], out)


class Config:
    """Resolved settings. Paths are expanded (relative ones resolve against the config file's folder) and the
    pacing keys become attributes: C.search_interval, C.max_active, ..."""

    def __init__(self, data=None, path=None):
        data = data or {}
        _warn_unknown(data)
        d = _merge(DEFAULTS, data)
        base = path.parent if path else pathlib.Path.cwd()
        self.path = path
        self.archive = _path(d["archive_dir"], base)
        self.work = _path(d["work_dir"], base)
        self.incoming = self.work / "incoming"
        self.incomplete = self.incoming / "incomplete"
        self.rejected = self.work / "rejected"
        self.share = self.work / "share"
        self.state_dir = _path(d["state_dir"], base)
        self.csv = _path(d["tracklist_csv"], base)
        self.inbox = _path(d["inbox_file"], base)
        self.local_import = _path(d["local_import_dir"], base)
        self.state_file = self.state_dir / "state.json"
        self.log_file = self.state_dir / "archivist.log"
        self.lock_file = self.state_dir / "archivist.lock"
        self.pid_file = self.state_dir / "archivist.pid"
        self.err_file = self.state_dir / "archivist.err"
        self.tree = _parse_tree(d["playlist_tree"])
        self.folders = {}                                   # playlist name -> archive folder (relative)
        _collect_folders(self.tree, [], self.folders)
        self.folders.update({k: str(v).strip("/") for k, v in (d["playlist_folders"] or {}).items()})
        self.sets = set(d["set_playlists"] or [])
        s = d["slskd"]
        self.slskd_url = os.environ.get("SLSKD_URL", s.get("url") or DEFAULTS["slskd"]["url"]).rstrip("/")
        self.api_key = os.environ.get("SLSKD_API_KEY") or s.get("api_key") or ""
        self.yml = _path(s.get("yml_path") or DEFAULTS["slskd"]["yml_path"], base)
        self.slskd_binary = _path(s.get("binary") or DEFAULTS["slskd"]["binary"], base)
        self.slskd_log = self.state_dir / "slskd.log"
        ff = _path(d["ffmpeg_dir"], base) if d.get("ffmpeg_dir") else None
        self.ffmpeg = str(ff / "ffmpeg") if ff and (ff / "ffmpeg").exists() else "ffmpeg"
        self.ffprobe = str(ff / "ffprobe") if ff and (ff / "ffprobe").exists() else "ffprobe"
        for k, v in d["pacing"].items():
            if not k.startswith("_"):
                setattr(self, k, v)
        self.max_rounds = len(self.retry_backoff)          # no usable file after this many rounds: not_found
        self.format_priority = [f for f in d["format_priority"] if f in FORMAT_KEYS]
        pats = [p for p in d["spam_tag_patterns"] or [] if p]
        self.spam_re = re.compile("|".join(f"(?:{p})" for p in pats) if pats else r"(?!)", re.I)
        self.marker = d["inbox_marker"] if (d["inbox_marker"] or "").strip() else DEFAULTS["inbox_marker"]
        self.marker_re = re.compile(r"\s*" + r"\s*".join(re.escape(t) for t in self.marker.split()) + r"\s*$", re.I)


C = None                                                   # the active Config (configure() / load_config())


def configure(data=None, path=None):
    global C, _HEADERS
    C = Config(data, path)
    _HEADERS = None
    return C


def load_config(path=None):
    """--config PATH, else $DJ_ARCHIVIST_CONFIG, else ~/.dj-archivist/config.json. A missing default file means
    defaults; a missing explicit file is an error."""
    explicit = path or os.environ.get("DJ_ARCHIVIST_CONFIG")
    p = pathlib.Path(explicit or DEFAULT_CONFIG).expanduser()
    if not p.exists():
        if explicit:
            sys.exit(f"config not found: {p}")
        return configure({}, None)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"invalid config {p}: {e}")
    return configure(data, p.resolve())


# ---------------------------------------------------------------- constants
COLUMNS = ["id", "artist", "title", "length", "bpm", "key", "playlists", "folder", "status", "file", "check",
           "notes", "source", "inbox_line"]
AUDIO_EXTS = {".mp3", ".m4a", ".aiff", ".aif", ".flac", ".wav"}
TAGGABLE = (".mp3", ".flac", ".aiff", ".aif", ".m4a")
PCM_RATES = (176400, 264600, 192000, 288000)   # bytes/s: 16/44.1, 24/44.1, 16/48, 24/48 stereo
FINISHED = ("done", "owned", "duplicate", "decision", "not_found")
KEEP_CHECK = ("decision:", "listen check")     # `check` notes that survive a new evaluation
FIT_ORDER = {"exact": 0, "size": 1, "weak": 2}
FRESH = 48 * 3600                  # a candidate last seen longer ago ranks lower
RETRY_AFTER = 24 * 3600            # a failed candidate (not permanent) may be tried again after this
QUOTA_SKIP = 24 * 3600             # "Too many megabytes/files": skip that user this long
REJECT_LIMIT, REJECT_WINDOW = 3, 7 * 24 * 3600   # users with 3 rejections, the last within 7 days, are skipped
HUGE_QUEUE_GIVE_UP = 600           # remote queue place > 2 x reachable_queue: give up after 10 min
MISSING_GRACE = 120                # transfer missing from the slskd list for this long: failed
CANARY_QUERY = "house music"       # a term everybody shares
EMPTY_STREAK = 3                   # empty results in a row before a canary search
CANARY_MIN_GAP = 600
CANARY_PAUSE = 15 * 60             # canary empty too: Soulseek is throttling, pause searches
THROTTLED_RETRY = 20 * 60          # a throttled search does not count as a round; retry the track after this
SEARCH_ERROR_RETRY = 600
CLEANUP_EVERY = 600
PLACEHOLDER_AGE = 3600             # zero-byte files in incomplete/ untouched this long are placeholders
PLAYLIST_SPLIT = re.compile(r"\s*[;|·]\s*")


# ---------------------------------------------------------------- helpers
def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    C.state_dir.mkdir(parents=True, exist_ok=True)
    with open(C.log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def mmss(s):
    return f"{int(s) // 60}:{int(s) % 60:02d}" if s else "?"


def parse_length(t):
    """'06:31', '6:31', '1:02:03' or '391' -> seconds. mm:ss gets +0.5: Rekordbox rounds seconds down, so the
    real length is centered half a second later."""
    t = (t or "").strip()
    if not t:
        return None
    if ":" in t:
        secs = 0
        for part in t.split(":"):
            secs = secs * 60 + int(part)
        return secs + 0.5
    return float(t)


def tol(s):
    """Length tolerance: 3 s, or 1% on long tracks."""
    return max(3.0, 0.01 * s)


def id_sort_key(i):
    return (0, int(i), "") if str(i).isdigit() else (1, 0, str(i))


def _under(p, root):
    try:
        return pathlib.Path(p).is_relative_to(root)
    except (AttributeError, ValueError):
        return False


@contextlib.contextmanager
def lock():
    C.state_dir.mkdir(parents=True, exist_ok=True)
    with open(C.lock_file, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_rows():
    if not C.csv.exists():
        return []
    with open(C.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r.pop(None, None)                          # extra cells of a malformed line must not become a column
        for a in COLUMNS:
            r[a] = r.get(a) or ""
        if r["status"] == "pending":
            r["status"] = ""
        if not r["folder"]:
            r["folder"] = primary_folder(r)
    return rows


def write_rows(rows):
    """Atomic write that keeps columns it does not know (a newer column added by hand or by another version
    must never be dropped). Skips the write when nothing changed."""
    extra = []
    for r in rows:
        extra += [k for k in r if k not in COLUMNS and k not in extra and k is not None]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS + extra, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    text = buf.getvalue()
    if C.csv.exists() and C.csv.read_text(encoding="utf-8-sig") == text:
        return
    C.csv.parent.mkdir(parents=True, exist_ok=True)
    tmp = C.csv.with_name("." + C.csv.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(C.csv)


def load_state():
    try:
        st = json.loads(C.state_file.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    for k, v in (("last_search", 0), ("last_enqueue", 0), ("empty_streak", 0), ("paused_until", 0),
                 ("last_canary", 0), ("users", {}), ("tracks", {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    C.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = C.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(C.state_file)


def track_state(st, tid):
    ts = st["tracks"].setdefault(tid, {})
    for k, v in (("round", 0), ("query_i", 0), ("next_search", 0), ("target", None), ("target_source", None),
                 ("candidates", {}), ("tried", {}), ("active", None), ("history", [])):
        ts.setdefault(k, v)
    return ts


def event(ts, msg):
    ts["history"] = (ts["history"] + [f"{datetime.now():%m-%d %H:%M} {msg}"])[-25:]


def playlists_of(r):
    return [p for p in PLAYLIST_SPLIT.split(r.get("playlists") or "") if p in C.folders]


def primary_folder(r):
    """A track lives in exactly one folder: its first non-set playlist, else its first set. Other memberships are
    linked as playlists in rekordbox.xml (a copied file would be a separate track with its own cues and analysis)."""
    pl = playlists_of(r)
    genre = [p for p in pl if p not in C.sets]
    first = (genre or pl or [None])[0]
    return C.folders.get(first, "_other")


def file_stem(row):
    name = f"{row['artist']} - {row['title']}" if row["artist"] else row["title"]
    return re.sub(r'[/:\\*?"<>|]', "-", name).strip()[:180]


def basename(fn):
    return fn.replace("\\", "/").split("/")[-1]


# ---------------------------------------------------------------- slskd API
_HEADERS = None


def api_headers():
    key = C.api_key
    if not key and C.yml.exists():
        m = re.search(r"^\s*key:\s*'?([^'\s]+)'?", C.yml.read_text(errors="replace"), re.M)
        key = m.group(1) if m and not re.fullmatch(r"[A-Z_]+", m.group(1)) else None   # skip placeholders
    if not key:
        sys.exit("no slskd API key: set slskd.api_key in the config, point slskd.yml_path at your slskd.yml, "
                 "or run slskd_setup.py")
    return {"X-API-Key": key}


def api(method, path, body=None):
    global _HEADERS
    if _HEADERS is None:
        _HEADERS = api_headers()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(C.slskd_url + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **_HEADERS})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def slskd_ready():
    try:
        return bool(api("GET", "/api/v0/server").get("isLoggedIn"))
    except Exception:
        return False


def search(query, wait=25, narrow=False):
    """Start a search, fetch the responses when it completes, then delete it from slskd. A "Completed" search can
    report responses before they are persisted: wait until the count matches, and do not delete early (an early
    delete returned an empty result in live use).
    narrow=True: a broad query with no artist word ("love songs"); response and file limits are halved. In live use
    the thousands of files returned by such a query were followed by slskd failing to start threads and crashing."""
    file_limit, response_limit = (250, 50) if narrow else (500, 100)
    sid = api("POST", "/api/v0/searches", {"searchText": query, "fileLimit": file_limit,
                                           "responseLimit": response_limit})["id"]
    for _ in range(wait):
        time.sleep(1)
        if "Completed" in str(api("GET", f"/api/v0/searches/{sid}").get("state", "")):
            break
    res = []
    for _ in range(5):
        time.sleep(2)
        res = api("GET", f"/api/v0/searches/{sid}/responses") or []
        if len(res) >= (api("GET", f"/api/v0/searches/{sid}").get("responseCount") or 0):
            break
    with contextlib.suppress(Exception):
        api("DELETE", f"/api/v0/searches/{sid}")
    return res


def delete_transfer(user, tid):
    if tid:
        with contextlib.suppress(Exception):
            api("DELETE", f"/api/v0/transfers/downloads/{urllib.parse.quote(user, safe='')}/{tid}?remove=true")


# ---------------------------------------------------------------- matching
NOISE_WORDS = {"extended", "original", "mix", "clean", "dirty", "intro", "edit", "remix", "the", "and", "feat", "ft",
               "vs", "x", "v1", "v2", "radio", "version", "vip"}
REMIX_WORDS = {"remix", "edit", "rework", "bootleg", "flip", "vip", "dub", "refix", "mix", "version", "remake"}
NEUTRAL_WORDS = NOISE_WORDS | {"club", "full", "long", "short", "12", "inch", "main", "album", "single", "master",
                               "mastered", "remaster", "remastered", "explicit", "outro", "vocal", "ext", "dj", "tool",
                               "hq", "promo", "instrumental", "acapella", "free", "download"}
LABEL_RE = re.compile(r"\b(extended|original|club|radio|dub|intro|instrumental|acapella|clean|dirty|edit|remix|mix)\b")
COMMON_WORDS = {"house", "music", "love", "life", "night", "time", "feel", "baby", "girl", "dance", "body", "one",
                "more", "beat", "goes", "on", "me", "my", "you", "your", "we", "it", "in", "of", "to", "for", "with",
                "no", "go", "get", "up", "down", "all", "day", "way", "like", "just", "make", "let", "this", "that",
                "dj", "club", "deep", "tech", "is", "be", "do", "so", "oh", "yeah", "can", "cant", "dont", "want",
                "need", "got", "into", "out", "what", "how", "who", "at", "by", "from", "back", "good", "bad", "new",
                "old", "big", "little", "right", "left", "move", "work", "party", "free", "high", "low", "hot", "come",
                "take", "give", "keep", "stop", "show", "play", "uk", "us", "ita", "de", "la", "el", "le", "da", "di",
                "del", "des", "and", "the", "or"}


def norm(s):
    """Lowercase words. Apostrophes vanish and dotted abbreviations collapse: "W.T.F." -> "wtf"."""
    s = re.sub(r"['’]", "", (s or "").lower())
    s = re.sub(r"(?<![a-z0-9])((?:[a-z0-9]\.){2,}[a-z0-9]?)(?![a-z0-9])", lambda m: m.group(1).replace(".", ""), s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def words(t):
    return [w for w in norm(t).split() if len(w) > 1 and w not in NOISE_WORDS and not re.fullmatch(r"\d+[ab]?", w)]


def split_row(row):
    """(artist word groups, core title words, bracket words: remixer and such). A title written as
    "Original Artist - Song" adds the original artist to the artists and keeps only the song as the core."""
    title = row["title"]
    artists = [words(a) for a in re.split(r",|&| x | feat\.? ", row["artist"])]
    if " - " in title:
        before, title = title.split(" - ", 1)
        artists.append(words(before))
    brackets = words(" ".join(re.findall(r"[\(\[](.*?)[\)\]]", title)))
    core = words(re.sub(r"[\(\[].*?[\)\]]", " ", title))
    return [a for a in artists if a], core, brackets


def name_queries(row):
    """Soulseek matches every word against the file path; "-", too many words and server-banned terms (some artist
    names) return nothing. Name variants: first artist + song + remixer, first artist + song, remixer + song,
    song alone."""
    artists, core, brackets = split_row(row)
    first = artists[0][:2] if artists else []
    out = []
    for q in (first + core + brackets[:2], first + core, brackets[:2] + core, core if len(core) >= 2 else []):
        q = " ".join(list(dict.fromkeys(q))[:6])
        if q and q not in out:
            out.append(q)
    return out


def keyword_words(row):
    """(name words, title words), most distinctive first. Priority: the row's artist and remixer (the owner of
    this version) > the original artist ("X - Song" titles) > long words and words with digits; common words
    sink to the end."""
    title = row["title"]
    original = words(title.split(" - ", 1)[0]) if " - " in title else []
    _, core, brackets = split_row(row)
    owner = list(dict.fromkeys(words(row["artist"]) + brackets))
    names = list(dict.fromkeys(owner + original))

    def score(w):
        return (len(w) + (6 if w in owner else 2 if w in original else 0) + (2 if re.search(r"\d", w) else 0)
                - (12 if w in COMMON_WORDS else 0))
    name_words = sorted(names, key=lambda w: (-score(w), names.index(w)))
    title_words = sorted([w for w in dict.fromkeys(core) if w not in names], key=lambda w: (-score(w), core.index(w)))
    return name_words, title_words


def keyword_queries(row):
    """When the full name does not surface the wanted file: keywords in combinations and alone. Order: name +
    title word, name + two title words, title triple and pairs, single words last. A common word (my, life,
    house...) is never searched alone or only with another common word; two-letter words stay out of
    combinations. Every file found this way still passes the same identity and length checks: the search gets
    looser, the matching does not."""
    name_words, title_words = keyword_words(row)
    title_words = [w for w in title_words if len(w) >= 3] or title_words   # "on", "me", "my" do not help
    name_words, title_words = name_words[:3], title_words[:4]

    def common(*ws):
        return all(w in COMMON_WORDS for w in ws)
    out = [f"{a} {b}" for a in name_words for b in title_words if not common(a, b)]
    out += [f"{a} {title_words[0]} {title_words[1]}" for a in name_words[:2]
            if len(title_words) >= 2 and not common(a, title_words[0], title_words[1])]
    if len(title_words) >= 3 and not common(*title_words[:3]):
        out.append(" ".join(title_words[:3]))
    out += [f"{title_words[i]} {title_words[j]}" for i in range(len(title_words))
            for j in range(i + 1, len(title_words)) if not common(title_words[i], title_words[j])]
    out += [w for w in name_words if w not in COMMON_WORDS and (len(w) >= 4 or re.search(r"\d", w))]
    out += [w for w in title_words if w not in COMMON_WORDS and (len(w) >= 7 or re.search(r"\d", w))]
    return list(dict.fromkeys(out))


def queries_for_round(row, rnd=0):
    """One round's queries: the name variants first (Soulseek is inconsistent, so every round), then this round's
    slice of the keyword combinations (each round a different slice)."""
    names = name_queries(row)
    kw = [q for q in keyword_queries(row) if q not in names]
    if not kw:
        return names
    start = (rnd * C.keyword_queries_per_round) % len(kw)
    return names + (kw[start:] + kw[:start])[:C.keyword_queries_per_round]


def other_remix(row, name):
    """A bracket in the file name says remix/edit/mix and carries a name that is not in the title or artist
    (e.g. "Someone Extended Mix"): that is another person's remix."""
    known = set(norm(row["artist"] + " " + row["title"]).split())
    for p in re.findall(r"[\(\[](.*?)[\)\]]", name):
        w = norm(p).split()
        if set(w) & REMIX_WORDS:
            foreign = [x for x in w if len(x) > 1 and x not in NEUTRAL_WORDS and x not in REMIX_WORDS
                       and x not in known and not re.fullmatch(r"\d+[ab]?", x)]
            if foreign:
                return True
    return False


def matches(row, path):
    """Strict identity: every core title word and remixer word in the FILE NAME; every word of at least one
    artist anywhere in the path (folders included); not somebody else's remix."""
    artists, core, brackets = split_row(row)
    path = path.replace("\\", "/")
    name = path.split("/")[-1]
    in_name = set(norm(name).split())
    if not all(w in in_name for w in core + brackets):
        return False
    in_path = set(norm(path).split())
    if artists and not any(all(w in in_path for w in a) for a in artists):
        return False
    return not other_remix(row, name)


def clean_dirty_mismatch(row, name):
    """1 when the title says clean and the file is dirty/explicit (or the other way round)."""
    t, a = set(norm(row["title"]).split()), set(norm(name).split())
    return int(bool(("clean" in t and {"dirty", "explicit"} & a and "clean" not in a) or
                    ("dirty" in t and "clean" in a and "dirty" not in a)))


def labels(name):
    """Version labels inside brackets: extended, original, club, radio, dub, edit, remix, mix..."""
    return set(LABEL_RE.findall(norm(" ".join(re.findall(r"[\(\[](.*?)[\)\]]", name)))))


# ---------------------------------------------------------------- candidates and the version decision
def format_key(c):
    """Format class of a candidate, or None when it must not be taken. MP3 only as 320 CBR, M4A from 256 kbps."""
    e = c["ext"]
    if e == ".mp3":
        return None if c.get("vbr") or (c.get("br") and c["br"] != 320) else "mp3_320"
    if e in (".aiff", ".aif"):
        return "aiff"
    if e in (".flac", ".wav"):
        return e[1:]
    if e == ".m4a":
        return None if c.get("br") and c["br"] < 256 else "m4a_256"
    return None


def format_rank(c):
    """Position in format_priority (smaller is better); None when the format is refused or not listed."""
    k = format_key(c)
    return C.format_priority.index(k) if k in C.format_priority else None


def fit(c, target):
    """Does the candidate fit the target length: 'exact' (length reported by the peer) | 'size' (computed from
    the file size) | 'weak' (size gives only a coarse band) | None."""
    t = tol(target)
    if c.get("len"):
        return "exact" if abs(c["len"] - target) <= t else None
    s, e = c.get("size") or 0, c["ext"]
    if e in (".aiff", ".aif", ".wav"):
        return "size" if any(abs(s / b - target) <= t + 2 for b in PCM_RATES) else None
    if e == ".mp3":      # 320 CBR: 40 000 B/s of audio + 0-4 MB of tags and cover art
        return "size" if (s - 4_000_000) / 40000 - t <= target <= s / 40000 + t else None
    if e == ".flac":     # compression from 35% (16 bit) to 85% (24 bit): only a coarse filter
        return "weak" if s / (264600 * 0.85) - t <= target <= s / (176400 * 0.35) + t else None
    if e == ".m4a":
        return "weak" if (s - 4_000_000) / 40000 - t <= target <= s / 30000 + t else None
    return None


def cluster_length(c):
    """Length usable for clustering: reported by the peer, or exact from the size of 16/44.1 PCM."""
    if c.get("len"):
        return float(c["len"])
    if c["ext"] in (".aiff", ".aif", ".wav") and c.get("size"):
        return c["size"] / 176400
    return None


def clusters(cands):
    """Group candidates of known length within the tolerance; per cluster the number of sources (users) and the
    version labels seen in the file names. Most sources first."""
    points = sorted(((L, i, c) for i, c in enumerate(cands) if (L := cluster_length(c))), key=lambda x: x[0])
    out = []
    for L, _, c in points:
        if out and L - out[-1]["center"] <= tol(L):
            k = out[-1]
            k["members"].append(c)
            k["center"] = sorted(cluster_length(x) for x in k["members"])[len(k["members"]) // 2]
        else:
            out.append({"center": L, "members": [c]})
    for k in out:
        k["sources"] = len({c["user"] for c in k["members"]})
        k["labels"] = sorted({e for c in k["members"] for e in labels(basename(c["fn"]))})
    return sorted(out, key=lambda k: -k["sources"])


def find_target(row, ts):
    """Target length: decision > reference length (tracklist) > confident cluster. (seconds, source, text)."""
    if ts.get("target_source") == "decision" and ts.get("target"):
        return ts["target"], "decision", "decision"
    ref = parse_length(row["length"])
    if ref:
        return ref, "reference", f"reference length {row['length']}"
    title_labels = labels(row["title"]) | ({"extended"} if "extended" in norm(row["title"]).split() else set())
    # Inbox items get extra care: only an explicit version name (extended/original/club) counts
    required = {"extended", "original", "club"} if row.get("inbox_line") else \
        {"extended", "original", "club", "mix", "edit", "remix", "dub"}
    confident = []
    for k in clusters(list(ts["candidates"].values())):
        lab = set(k["labels"])
        if k["sources"] >= 2 and lab & required and not ("radio" in lab and "radio" not in title_labels):
            confident.append(k)
    preferred = [k for k in confident if title_labels & (set(k["labels"]) - {"mix", "edit", "remix"})] or confident
    if len(preferred) == 1:
        k = preferred[0]
        return k["center"], "cluster", f"{k['sources']} sources at {mmss(k['center'])} ({', '.join(k['labels'])})"
    return None, None, (f"{len(preferred)} confident clusters, choice unclear" if preferred
                        else "no consistent length")


def ranked_candidates(st, row, ts, target, now):
    """Usable candidates, best first: reachability, format priority, clean/dirty, length evidence, freshness,
    the user's rejections, free slot, queue length, speed."""
    out = []
    for key, c in ts["candidates"].items():
        if not c:
            continue
        t = ts["tried"].get(key)
        if t and (t.get("permanent") or now < t.get("retry_after", 0)):
            continue
        rank = format_rank(c)
        f = fit(c, target) if (target and rank is not None) else None
        if not f:
            continue
        us = st["users"].get(c["user"], {})
        if us.get("banned") or now < us.get("skip_until", 0):
            continue
        if us.get("rejects", 0) >= REJECT_LIMIT and now - us.get("last_reject", 0) < REJECT_WINDOW:
            continue
        reachable = bool(c.get("free_slot")) or (c.get("queue") or 0) <= C.reachable_queue
        out.append(((0 if reachable else 1, rank, clean_dirty_mismatch(row, basename(c["fn"])), FIT_ORDER[f],
                     0 if now - c.get("last_seen", 0) <= FRESH else 1, us.get("rejects", 0),
                     0 if c.get("free_slot") else 1, c.get("queue") or 0, -(c.get("speed") or 0)), key))
    out.sort()
    return [key for _, key in out]


def record_candidates(row, ts, responses, now):
    """Merge the matching audio files of a search into the track's evidence. Returns the number of new ones."""
    new = 0
    for r in responses:
        for f in r.get("files", []):
            fn = f["filename"]
            ext = pathlib.PurePosixPath(fn.replace("\\", "/")).suffix.lower()
            if ext not in AUDIO_EXTS or f.get("isLocked") or not matches(row, fn):
                continue
            key = r["username"] + "|" + fn
            c = ts["candidates"].get(key)
            if not c:
                c = ts["candidates"][key] = {"user": r["username"], "fn": fn, "ext": ext, "first_seen": now,
                                             "seen": 0}
                new += 1
            c["size"] = f.get("size") or c.get("size") or 0
            for a, b in (("br", "bitRate"), ("vbr", "isVariableBitRate"), ("len", "length")):
                if f.get(b) is not None:
                    c[a] = f[b]
            c.update(free_slot=r.get("hasFreeUploadSlot"), queue=r.get("queueLength", 0),
                     speed=r.get("uploadSpeed", 0), last_seen=now)
            c["seen"] += 1
    return new


# ---------------------------------------------------------------- files: verify, tag, file into the archive
def probe(path):
    out = subprocess.run([C.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(path)], capture_output=True, text=True, timeout=120).stdout
    return json.loads(out or "{}")


def verify(path, target):
    """(ok, info, reason). Length within the tolerance, MP3 316-324 kbps, AAC >= 250 kbps, known codecs only."""
    try:
        p = probe(path)
    except Exception as e:
        return False, {}, f"ffprobe: {e}"
    a = next((s for s in p.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not a:
        return False, {}, "no audio stream"
    dur = float(p.get("format", {}).get("duration") or 0)
    codec = a.get("codec_name", "")
    br = int(a.get("bit_rate") or p.get("format", {}).get("bit_rate") or 0)
    info = {"codec": codec, "length": dur, "kbps": br // 1000, "sample_rate": a.get("sample_rate"),
            "bits": a.get("bits_per_raw_sample") or a.get("bits_per_sample")}
    if target and abs(dur - target) > tol(target):
        return False, info, f"length {mmss(dur)}, target {mmss(target)}"
    if codec == "mp3" and not 316_000 <= br <= 324_000:
        return False, info, f"MP3 {br // 1000} kbps (320 only)"
    if codec == "aac" and br < 250_000:
        return False, info, f"AAC {br // 1000} kbps"
    if not (codec in ("mp3", "aac", "flac", "alac") or codec.startswith("pcm_")):
        return False, info, f"codec {codec}"
    return True, info, ""


def weak_high_band(path):
    """Upsample hint for MP3: the 17.5-19.5 kHz band more than 34 dB below the 12-15 kHz band suggests a file
    inflated from 128 kbps. Calibration: a real 320 measured about -27 dB, one inflated from 128 about -39, one
    from 192 about -31 (cannot be told apart). Only adds a note, never rejects. (suspect, difference in dB)."""
    def band(f):
        out = subprocess.run([C.ffmpeg, "-v", "info", "-i", str(path), "-map", "0:a", "-af",
                              f + ",astats=measure_perchannel=none:measure_overall=RMS_level", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=300).stderr
        m = re.findall(r"RMS level dB: (-?[\d.]+|-inf)", out)
        return float(m[-1]) if m and m[-1] != "-inf" else -150.0
    try:
        high = band("highpass=f=17500,highpass=f=17500,highpass=f=17500,highpass=f=17500,"
                    "lowpass=f=19500,lowpass=f=19500")
        ref = band("highpass=f=12000,highpass=f=12000,lowpass=f=15000,lowpass=f=15000")
        return high - ref < -34, round(high - ref, 1)
    except Exception:
        return False, None


def packet_md5(path):
    """md5 of the audio packets, without decoding. Tags are written with -c copy, so equal packets mean equal
    audio. A decoded md5 may differ even then: ffmpeg writes a new Xing/LAME header and the gapless trim moves."""
    out = subprocess.run([C.ffmpeg, "-v", "quiet", "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
                         capture_output=True, text=True, timeout=600).stdout.strip()
    return out or None


def tag_copy(src, dst, row):
    """Copy with tags: artist/title from the tracklist (Rekordbox shows these), spam tags blanked. The audio packets
    are untouched (-c copy); when their md5 does not match (or cannot be read), a plain untagged copy instead."""
    ext = src.suffix.lower()
    if ext in TAGGABLE:
        tmp = dst.with_name(dst.stem + ".tagging" + ext)
        cmd = [C.ffmpeg, "-v", "error", "-y", "-i", str(src), "-map", "0", "-c", "copy"]
        with contextlib.suppress(Exception):
            for k, v in (probe(src).get("format", {}).get("tags") or {}).items():
                if C.spam_re.search(str(v)):
                    cmd += ["-metadata", f"{k}="]
        if row["artist"]:
            cmd += ["-metadata", f"artist={row['artist']}"]
        cmd += ["-metadata", f"title={row['title']}"]
        if ext == ".mp3":
            cmd += ["-id3v2_version", "3", "-write_id3v1", "0"]
        if ext in (".aiff", ".aif"):
            cmd += ["-write_id3v2", "1"]
        cmd.append(str(tmp))
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            before = packet_md5(src)
            if before and before == packet_md5(tmp):
                tmp.replace(dst)
                return "tagged"
        except Exception:
            pass
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    shutil.copy2(src, dst)
    return "copied untagged"


def unique_path(p):
    """p, or p with ' (2)', ' (3)'... when it exists (so nothing is ever overwritten)."""
    n = 2
    out = p
    while out.exists():
        out = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return out


def file_track(src, row, move=True):
    """Tag and place one file: <archive>/<folder>/Artist - Title.ext. move=False leaves the source untouched."""
    folder = C.archive / row["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / (file_stem(row) + src.suffix.lower())
    if dst.exists():
        dst = folder / (f"{file_stem(row)} [{row['id']}]" + src.suffix.lower())
    how = tag_copy(src, dst, row)
    if move:
        src.unlink()
        if src.parent != C.incoming:
            with contextlib.suppress(OSError):
                src.parent.rmdir()                  # only when empty
    return dst, how


def cleanup_placeholders():
    """Remove the empty folders slskd leaves in incoming/ and the zero-byte placeholders of refused transfers in
    incomplete/ (untouched for an hour). Never touches a file with data; incoming/, incomplete/ and rejected/
    themselves stay."""
    keep = {C.incoming, C.incomplete, C.rejected}
    if C.incomplete.exists():
        for f in C.incomplete.rglob("*"):
            with contextlib.suppress(OSError):
                if f.is_file() and f.stat().st_size == 0 and time.time() - f.stat().st_mtime > PLACEHOLDER_AGE:
                    f.unlink()
    if C.incoming.exists():
        for d in sorted((p for p in C.incoming.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            if d not in keep:
                with contextlib.suppress(OSError):
                    d.rmdir()


def downloaded_path(f):
    """Where slskd put a finished transfer: incoming/<remote parent folder>/<name>, else anywhere in incoming/."""
    parts = f["filename"].replace("\\", "/").split("/")
    guess = C.incoming / parts[-2] / parts[-1] if len(parts) > 1 else C.incoming / parts[-1]
    if guess.exists():
        return guess
    return next((x for x in C.incoming.rglob("*") if x.name == parts[-1] and x.is_file()
                 and not _under(x, C.incomplete) and not _under(x, C.rejected)), None)


# ---------------------------------------------------------------- transfer tracking
def fail(st, row, ts, reason, permanent=True, now=None):
    """The active transfer failed: remember it on the candidate and the user, then back to `candidate` (the next
    candidate is tried). "Banned" bans the user for good; "Too many megabytes/files" skips the user for 24 h."""
    now = now or time.time()
    a = ts["active"]
    ts["tried"][a["key"]] = {"reason": reason, "t": now, "permanent": permanent, "retry_after": now + RETRY_AFTER}
    us = st["users"].setdefault(a["user"], {"rejects": 0, "successes": 0, "last_reject": 0})
    if permanent:
        us["rejects"] = us.get("rejects", 0) + 1
        us["last_reject"] = now
    if "Banned" in reason:                      # refuses accounts without shares: none of their files is tried
        us["banned"] = True
    elif re.search(r"Too many (megabytes|files)", reason):
        us["skip_until"] = now + QUOTA_SKIP
    event(ts, f"failed {a['user']}: {reason}")
    log(f"[{row['id']}] failed ({a['user']}): {reason}")
    ts["active"] = None
    row["status"] = "candidate"


def completed(st, row, ts, f, now):
    a = ts["active"]
    path = downloaded_path(f)
    delete_transfer(a["user"], a["id"])
    if not path:
        return fail(st, row, ts, "downloaded but the file was not found", now=now)
    ok, info, reason = verify(path, ts.get("target"))
    if not ok:
        C.rejected.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(unique_path(C.rejected / f"{row['id']}-{path.name}")))   # kept, never deleted
        if path.parent != C.incoming:
            with contextlib.suppress(OSError):
                path.parent.rmdir()
        return fail(st, row, ts, f"verification: {reason}", now=now)
    suspect, diff = weak_high_band(path) if info["codec"] == "mp3" else (False, None)
    mismatch = clean_dirty_mismatch(row, path.name)
    dst, how = file_track(path, row)
    us = st["users"].setdefault(a["user"], {"rejects": 0, "successes": 0, "last_reject": 0})
    us["successes"] = us.get("successes", 0) + 1
    summary = f"{info['codec']} {info['kbps']} kbps · {mmss(info['length'])} · {a['user']}"
    notes = [row["check"]] if row["check"].startswith("listen check") else []
    if mismatch:
        notes.append("clean/dirty is the opposite of the title")
    if suspect:
        notes.append(f"listen check: weak high frequencies ({diff} dB), may be upsampled from a lower bitrate")
    row.update(status="done", file=str(dst.relative_to(C.archive)), check=" · ".join(notes))
    c = ts["candidates"].get(a["key"])
    ts["candidates"] = {a["key"]: c} if c else {}             # keep the state file small
    ts["active"] = None
    ts["done_at"] = now
    cleanup_placeholders()
    event(ts, f"done: {summary} ({how})")
    log(f"[{row['id']}] DONE {dst.relative_to(C.archive)} ({summary}, {how})")


def watch(st, rows, now):
    """Follow the active transfers in slskd: finished ones are verified and filed; refused, errored, long-queued,
    never-started, stalled or missing ones fail over to the next candidate."""
    active = [(r, st["tracks"][r["id"]]) for r in rows if st["tracks"].get(r["id"], {}).get("active")]
    if not active:
        return
    try:
        transfers = api("GET", "/api/v0/transfers/downloads") or []
    except Exception as e:
        log(f"could not list transfers: {e}")
        return
    by_id = {f["id"]: f for u in transfers for d in u.get("directories", []) for f in d.get("files", [])}
    for row, ts in active:
        a = ts["active"]
        f = by_id.get(a["id"])
        if not f:
            if now - a["started"] > MISSING_GRACE:
                fail(st, row, ts, "missing from the slskd transfer list", permanent=False, now=now)
            continue
        state = f.get("state", "")
        a["state"] = state
        if state == "Completed, Succeeded":
            try:
                completed(st, row, ts, f, now)
            except Exception as e:
                if ts.get("active"):
                    fail(st, row, ts, f"processing error: {e}", permanent=False, now=now)
        elif state.startswith("Completed"):
            delete_transfer(a["user"], a["id"])
            fail(st, row, ts, f"{state} {f.get('exception') or ''}".strip(), now=now)
        elif "Remotely" in state:
            c = ts["candidates"].get(a["key"]) or {}
            place = f.get("placeInQueue") or (0 if c.get("free_slot") else c.get("queue")) or 0
            if place > 2 * C.reachable_queue and now - a["started"] > HUGE_QUEUE_GIVE_UP:
                delete_transfer(a["user"], a["id"])
                fail(st, row, ts, f"remote queue position {place}", permanent=False, now=now)
            elif now - a["started"] > C.remote_queue_timeout:
                delete_transfer(a["user"], a["id"])
                fail(st, row, ts, f"in the remote queue for more than {C.remote_queue_timeout // 60} min"
                     f" (position {f.get('placeInQueue', '?')})", permanent=False, now=now)
        elif state == "InProgress":
            b = f.get("bytesTransferred", 0)
            if b > a.get("bytes", 0):
                a["bytes"], a["progress_at"] = b, now
            elif now - a.get("progress_at", a["started"]) > C.stall_timeout:
                delete_transfer(a["user"], a["id"])
                fail(st, row, ts, "download stalled", permanent=False, now=now)
        elif now - a["started"] > C.start_timeout:
            delete_transfer(a["user"], a["id"])
            fail(st, row, ts, f"stuck in '{state}'", permanent=False, now=now)


def start_downloads(st, rows, now, allowed=None):
    """Enqueue the best candidate of `candidate` tracks (inbox tracks first). At most max_active transfers really
    downloading (remote-queued ones do not count) and max_open_requests open in total; one per user; enqueue_interval
    between two starts."""
    active = [st["tracks"][r["id"]]["active"] for r in rows if st["tracks"].get(r["id"], {}).get("active")]
    busy = {a["user"] for a in active}
    for row in sorted(rows, key=lambda r: 0 if r["inbox_line"] else 1):
        transferring = [a for a in active if "Remotely" not in (a.get("state") or "")]
        if (len(transferring) >= C.max_active or len(active) >= C.max_open_requests
                or now - st["last_enqueue"] < C.enqueue_interval):
            return
        if row["status"] != "candidate" or (allowed is not None and row["id"] not in allowed):
            continue
        ts = track_state(st, row["id"])
        if ts["active"]:
            continue
        order = ranked_candidates(st, row, ts, ts.get("target"), now)
        if not order:
            round_over(row, ts, now, "no usable candidate left")
            continue
        key = next((k for k in order if ts["candidates"][k]["user"] not in busy), None)
        if not key:
            continue
        c = ts["candidates"][key]
        st["last_enqueue"] = now
        try:
            r = api("POST", f"/api/v0/transfers/downloads/{urllib.parse.quote(c['user'], safe='')}",
                    [{"filename": c["fn"], "size": c["size"]}])
        except Exception as e:
            r = {"failed": [str(e)]}
        if not r or r.get("failed") or not r.get("enqueued"):
            ts["active"] = {"key": key, "user": c["user"], "id": None, "started": now}
            fail(st, row, ts, f"could not enqueue: {(r or {}).get('failed')}", permanent=False, now=now)
            continue
        ts["active"] = {"key": key, "user": c["user"], "id": r["enqueued"][0]["id"], "started": now,
                        "bytes": 0, "progress_at": now, "state": r["enqueued"][0].get("state")}
        row["status"] = "downloading"
        active.append(ts["active"])
        busy.add(c["user"])
        event(ts, f"download: {c['user']} · {basename(c['fn'])}")
        log(f"[{row['id']}] download started: {c['user']} · {basename(c['fn'])} ({c['size'] // 1_000_000} MB)")


# ---------------------------------------------------------------- search rounds
def round_over(row, ts, now, reason):
    ts["round"] += 1
    ts["query_i"] = 0
    if ts["round"] >= C.max_rounds:
        row["status"] = "not_found"
        row["check"] = f"not found in {ts['round']} rounds: {reason} (get it manually or buy it)"
        event(ts, "not found")
        log(f"[{row['id']}] not found: {reason}")
    else:
        row["status"] = "waiting"
        ts["next_search"] = now + C.retry_backoff[ts["round"]]
        event(ts, f"round {ts['round']} over ({reason}); next search "
                  f"{datetime.fromtimestamp(ts['next_search']):%m-%d %H:%M}")


def evaluate(st, row, ts, now, last_query):
    """After a search: target + usable candidate -> candidate; else the next query, the end of the round, or a
    decision request."""
    target, source, desc = find_target(row, ts)
    ts["target"], ts["target_source"] = target, source
    if target and ranked_candidates(st, row, ts, target, now):
        row["status"] = "candidate"
        ts["query_i"] = 0
        if not row["check"].startswith(KEEP_CHECK):
            row["check"] = ""
        return
    if not last_query:
        row["status"] = "waiting"
        ts["query_i"] += 1
        ts["next_search"] = now + C.next_variant_delay
        return
    cl = clusters(list(ts["candidates"].values()))
    if ts["round"] + 1 >= C.decision_after_rounds and cl and not ts.get("keep_waiting"):
        ts["round"] += 1
        ts["query_i"] = 0
        row["status"] = "decision"
        reason = (f"reference length {row['length']} not found, another version exists" if source == "reference"
                  else f"target unclear: {desc}")
        row["check"] = reason + " · " + ", ".join(f"{mmss(k['center'])} ({k['sources']} sources)" for k in cl[:4])
        event(ts, "decision: " + row["check"])
        log(f"[{row['id']}] DECISION needed: {row['check']}")
        return
    round_over(row, ts, now, desc if not target else f"no usable file at {mmss(target)}")


def next_search_row(st, rows, now, allowed=None):
    ready = [r for r in rows if r["status"] in ("", "waiting") and (allowed is None or r["id"] in allowed)
             and track_state(st, r["id"])["next_search"] <= now]
    ready.sort(key=lambda r: (r["status"] != "", 0 if r["inbox_line"] else 1, track_state(st, r["id"])["next_search"]))
    return ready[0] if ready else None


def canary(st):
    """Are the empty results real, or is the server throttling? Search a term everybody shares."""
    st["last_canary"] = time.time()
    try:
        n = len(search(CANARY_QUERY))
    except Exception:
        n = 0
    st["empty_streak"] = 0
    if n == 0:
        st["paused_until"] = time.time() + CANARY_PAUSE
        log(f"canary search empty: Soulseek is throttling, searches paused for {CANARY_PAUSE // 60} min")
        return True
    log(f"canary search: {n} users, the empty results were real")
    return False


def do_search(tid):
    """One search (the network part runs outside the lock), then merge the evidence and evaluate."""
    with lock():
        st, rows = load_state(), read_rows()
        row = next(r for r in rows if r["id"] == tid)
        ts = track_state(st, tid)
        variants = queries_for_round(row, ts["round"]) or [row["title"]]
        i = ts["query_i"] % len(variants)
        query = variants[i]
        st["last_search"] = time.time()
        save_state(st)
    artist_words = {w for group in split_row(row)[0] for w in group}
    try:
        res, err = search(query, narrow=not (artist_words & set(query.split()))), None
    except Exception as e:
        res, err = [], str(e)
    with lock():
        now = time.time()
        st, rows = load_state(), read_rows()
        row = next((r for r in rows if r["id"] == tid), None)
        if row is None:                                      # removed from the tracklist meanwhile
            return
        ts = track_state(st, tid)
        if err:
            event(ts, f"search error '{query}': {err}")
            log(f"[{tid}] search error '{query}': {err}")
            ts["next_search"] = now + SEARCH_ERROR_RETRY
            save_state(st)
            return
        st["empty_streak"] = st["empty_streak"] + 1 if not res else 0
        new = record_candidates(row, ts, res, now)
        event(ts, f"search '{query}': {len(res)} users, {new} new candidates")
        log(f"[{tid}] search '{query}': {len(res)} users, {new} new candidates (total {len(ts['candidates'])})")
        if not res and st["empty_streak"] >= EMPTY_STREAK and now - st["last_canary"] > CANARY_MIN_GAP \
                and canary(st):
            ts["next_search"] = time.time() + THROTTLED_RETRY     # a throttled search does not count as a round
            save_state(st)
            return
        evaluate(st, row, ts, now, last_query=i + 1 >= len(variants))
        write_rows(rows)
        save_state(st)


# ---------------------------------------------------------------- the loop
def single_instance():
    """Hold an exclusive lock on the pid file for the life of the loop (a second loop exits)."""
    C.state_dir.mkdir(parents=True, exist_ok=True)
    f = open(C.pid_file, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"archivist is already running ({C.pid_file})")
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def running_pid():
    """pid of the running loop, or None. The pid file lock tells, so a stale pid file is never trusted."""
    try:
        with open(C.pid_file) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return int(f.read().strip() or 0) or None
            fcntl.flock(f, fcntl.LOCK_UN)
            return None
    except (OSError, ValueError):
        return None


def ensure_slskd():
    if subprocess.run(["pgrep", "-f", str(C.slskd_binary)], capture_output=True).returncode != 0:
        log("slskd is not running, starting it")
        cmd = [sys.executable, str(HERE / "slskd_setup.py"), "--start"]
        if C.path:
            cmd += ["--config", str(C.path)]
        subprocess.run(cmd, capture_output=True, timeout=180)


def run_loop(limit=None):
    """The loop: follow transfers, start downloads, one search per search_interval, housekeeping. With `limit`,
    only the first N open tracks, and exit when each is settled or has finished a round."""
    pid_lock = single_instance()
    allowed = None
    with lock():
        rows = read_rows()
        write_rows(rows)                                  # fills derived columns (folder) into the file
    if limit:
        open_ids = [r["id"] for r in rows if r["status"] in ("", "waiting", "candidate", "downloading")]
        allowed = set(sorted(open_ids, key=id_sort_key)[:limit])
        log(f"round started: {len(allowed)} track{'s' * (len(allowed) != 1)} ({', '.join(sorted(allowed, key=id_sort_key))})")
    else:
        log("loop started")
    down = 0
    while True:
        to_search, finished = None, False
        try:
            if not slskd_ready():
                down += 1
                if down in (1, 5, 20):
                    ensure_slskd()
                time.sleep(60 if down < 5 else 300)
                continue
            down = 0
            now = time.time()
            with lock():
                st, rows = load_state(), read_rows()
                watch(st, rows, now)
                start_downloads(st, rows, now, allowed)
                write_rows(rows)
                save_state(st)
                if now - st["last_search"] >= C.search_interval and now >= st["paused_until"]:
                    to_search = next_search_row(st, rows, now, allowed)
                if now - st.get("last_cleanup", 0) > CLEANUP_EVERY:
                    st["last_cleanup"] = now
                    cleanup_placeholders()
                    save_state(st)
                if any(r["inbox_line"] and r["status"] in ("done", "owned") for r in rows):
                    mark_inbox()
                count = sum(1 for r in rows if r["status"] in ("done", "owned") and r["file"])
                if count != st.get("index_count"):
                    st["index_count"] = count
                    with contextlib.redirect_stdout(io.StringIO()):
                        export_rekordbox()
                    save_state(st)
                finished = allowed is not None and all(
                    r["status"] in FINISHED or (r["status"] == "waiting" and track_state(st, r["id"])["round"] >= 1)
                    for r in rows if r["id"] in allowed)
            if to_search:
                do_search(to_search["id"])
            elif finished:
                log("round finished")
                break
        except KeyboardInterrupt:
            break
        except Exception:
            log("ERROR " + traceback.format_exc().strip().replace("\n", " | ")[-800:])
            time.sleep(60)
        time.sleep(C.loop)
    pid_lock.close()


# ---------------------------------------------------------------- commands: loop control
def cmd_start():
    """Start the loop as a background process, independent of this terminal (it stops on reboot)."""
    pid = running_pid()
    if pid:
        print(f"loop already running (pid {pid})")
        return
    C.state_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(pathlib.Path(__file__).resolve())]
    if C.path:
        cmd += ["--config", str(C.path)]
    cmd.append("run")
    with open(C.err_file, "a") as err:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                             start_new_session=True)
    time.sleep(3)
    if p.poll() is not None:
        print(f"loop exited right away (code {p.returncode}); see {C.err_file}")
        return
    print(f"loop started (pid {p.pid}); follow it with: archivist.py status")


def cmd_stop():
    pid = running_pid()
    if not pid:
        print("loop not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        time.sleep(0.5)
        if not running_pid():
            break
    print("loop stopped (active transfers continue in slskd; the loop picks them up when it starts again)")


def cmd_status():
    st, rows = load_state(), read_rows()
    counts = {}
    for r in rows:
        counts[r["status"] or "pending"] = counts.get(r["status"] or "pending", 0) + 1
    print("status:", " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])) or "no tracks")
    now = time.time()
    for r in rows:
        a = st["tracks"].get(r["id"], {}).get("active")
        if a:
            print(f"  downloading [{r['id']}] {file_stem(r)[:60]} <- {a['user']} · {a.get('state')} · "
                  f"{int((now - a['started']) // 60)} min")
    waits = [st["tracks"].get(r["id"], {}).get("next_search", 0) for r in rows if r["status"] == "waiting"]
    if waits:
        print(f"waiting {len(waits)}: {sum(1 for w in waits if w <= now)} searchable now, the last at "
              f"{datetime.fromtimestamp(max(waits)):%d.%m %H:%M}")
    if now < st.get("paused_until", 0):
        print(f"searches paused until {datetime.fromtimestamp(st['paused_until']):%H:%M} (Soulseek is throttling)")
    pid = running_pid()
    print(f"loop running (pid {pid})" if pid else "loop not running: archivist.py start")
    print("slskd:", "connected" if slskd_ready() else "NOT CONNECTED (slskd_setup.py --start)")
    if C.log_file.exists():
        print("recent events:")
        for s in C.log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]:
            print("  " + s)


# ---------------------------------------------------------------- commands: decisions
def cmd_decisions():
    st, rows = load_state(), read_rows()
    n = 0
    for r in rows:
        if r["status"] != "decision":
            continue
        n += 1
        ts = track_state(st, r["id"])
        print(f"\n[{r['id']}] {file_stem(r)}\n  reference: {r['length'] or 'none'} · bpm {r['bpm'] or '?'} · "
              f"key {r['key'] or '?'} · {r['folder']}\n  {r['check']}")
        cands = [c for c in ts["candidates"].values() if c]
        for k in clusters(cands):
            examples = sorted({basename(c["fn"]) for c in k["members"]})[:3]
            fmts = sorted({c["ext"] for c in k["members"]})
            print(f"  {mmss(k['center'])}: {k['sources']} sources, {len(k['members'])} files, labels {k['labels']}, "
                  f"formats {fmts}")
            for e in examples:
                print(f"      {e}")
        unknown = [c for c in cands if not cluster_length(c)]
        if unknown:
            sizes = sorted({f"{(c.get('size') or 0) // 1_000_000} MB {c['ext']}" for c in unknown})[:6]
            print(f"  {len(unknown)} file(s) of unknown length (MP3/FLAC sizes: {', '.join(sizes)})")
    print(f"\n{n} decision(s) pending. Decide with: archivist.py decide ID mm:ss|wait|skip")


def cmd_decide(tid, value):
    """mm:ss: take the version of that length; wait: keep waiting for the reference length; skip: not_found
    (get it manually or buy it)."""
    value = value.strip().lower()
    if value not in ("wait", "skip"):
        try:
            target = parse_length(value)
        except ValueError:
            target = None
        if not target:
            sys.exit(f"'{value}' is not a length (mm:ss), 'wait' or 'skip'")
    with lock():
        st, rows = load_state(), read_rows()
        row = next((r for r in rows if r["id"] == tid), None)
        if not row:
            sys.exit(f"no track with id {tid}")
        ts = track_state(st, tid)
        if value == "skip":
            row.update(status="not_found", check="decision: skipped (get it manually or buy it)")
            event(ts, "decision: skip")
        elif value == "wait":
            ts["keep_waiting"] = True
            ts["next_search"] = time.time() + C.retry_backoff[min(ts["round"], C.max_rounds - 1)]
            row.update(status="waiting", check="decision: keep waiting for the reference length")
            event(ts, "decision: wait")
        else:
            ts["target"], ts["target_source"] = target, "decision"
            ts["next_search"] = 0
            ok = ranked_candidates(st, row, ts, target, time.time())
            row.update(status="candidate" if ok else "", check=f"decision: {value}")
            event(ts, f"decision: {value} ({len(ok)} usable candidates)")
            print(f"[{tid}] target {value}: {len(ok)} usable candidates -> "
                  f"{'will download' if ok else 'will search again'}")
        write_rows(rows)
        save_state(st)


# ---------------------------------------------------------------- commands: archive maintenance
def cmd_import_local(dry_run=False, ids=None):
    """`owned` tracks: the matching file in local_import_dir is copied (the original stays untouched). Other open
    tracks: an orphan download in incoming/ is moved. A local file whose length does not match the reference is
    still filed, with a note; an orphan download must verify."""
    with lock():
        st, rows = load_state(), read_rows()
        incoming = [p for p in C.incoming.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
                    and not _under(p, C.incomplete) and not _under(p, C.rejected)] if C.incoming.exists() else []
        local = [p for p in C.local_import.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS] \
            if C.local_import.exists() else []
        active = {basename(st["tracks"][r["id"]]["active"]["key"].split("|", 1)[1])
                  for r in rows if st["tracks"].get(r["id"], {}).get("active")}
        for r in rows:
            if r["status"] in ("done", "duplicate") or (r["file"] and (C.archive / r["file"]).exists()):
                continue
            if ids and r["id"] not in ids:
                continue
            pool = local if r["status"] == "owned" else [p for p in incoming if p.name not in active]
            found = [p for p in pool if p.exists() and matches(r, p.name)]
            if not found:
                if r["status"] == "owned":
                    print(f"[{r['id']}] owned but not found in {C.local_import}: {file_stem(r)}")
                continue
            ref = parse_length(r["length"])
            chosen, info, reason = None, {}, ""
            for p in found:
                ok, info, reason = verify(p, ref)
                if ok:
                    chosen = p
                    break
            if not chosen and r["status"] != "owned":
                print(f"[{r['id']}] in incoming/ but not verified: {found[0].name} ({reason})")
                continue
            p = chosen or found[0]
            if not chosen:
                info = verify(p, None)[1]
            note = f"local file: {reason}" if not chosen else ""
            print(f"[{r['id']}] {p.name} -> {r['folder']}/ ({info.get('codec')} {info.get('kbps')} kbps, "
                  f"{mmss(info.get('length'))}{', ' + note if note else ''})")
            if dry_run:
                continue
            dst, how = file_track(p, r, move=(r["status"] != "owned"))
            r["file"] = str(dst.relative_to(C.archive))
            r["check"] = note
            if r["status"] != "owned":
                r["status"] = "done"
            event(track_state(st, r["id"]), f"import-local: {p.name} ({how})")
            log(f"[{r['id']}] import-local: {p.name} -> {r['file']} ({how})")
        if not dry_run:
            write_rows(rows)
            save_state(st)


def cmd_retag():
    """Retag archive files in place when artist/title differ from the tracklist or a tag carries spam (audio
    packets md5-checked, as when filing)."""
    with lock():
        for r in read_rows():
            if r["status"] == "duplicate" or not r["file"] or not (C.archive / r["file"]).exists():
                continue
            p = C.archive / r["file"]
            if p.suffix.lower() not in TAGGABLE:
                continue
            tags = {k.lower(): v for k, v in (probe(p).get("format", {}).get("tags") or {}).items()}
            spam = any(C.spam_re.search(str(v)) for v in tags.values())
            if tags.get("title") == r["title"] and (not r["artist"] or tags.get("artist") == r["artist"]) \
                    and not spam:
                continue
            old = unique_path(p.with_name(p.stem + ".old" + p.suffix))
            p.rename(old)
            how = tag_copy(old, p, r)
            old.unlink()
            log(f"[{r['id']}] retag: {r['file']} ({how})")


# ---------------------------------------------------------------- inbox: the plain-text request list
BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")


def _split_lines(text):
    """Lines with their endings kept; only "\\n" ends a line, so joining them gives back the exact text."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def inbox_key(t):
    return re.sub(r"\s+", " ", BULLET_RE.sub("", (t or "").strip())).lower()


def read_inbox():
    """Inbox -> items {line, text, heading, marked}. A heading is a line ending with ':' that has no ' - ', or a
    line starting with '#' (genre, where the track was heard...); it applies to the items below it."""
    if not C.inbox.exists():
        return []
    out, heading = [], ""
    for i, raw in enumerate(_split_lines(C.inbox.read_bytes().decode("utf-8", errors="replace"))):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#") or (s.endswith(":") and " - " not in s):
            heading = s.lstrip("#").rstrip(":").strip()
            continue
        out.append({"line": i + 1, "text": C.marker_re.sub("", BULLET_RE.sub("", s)).strip(), "heading": heading,
                    "marked": bool(C.marker_re.search(s))})
    return out


def mark_lines(raw, done_keys):
    """(new bytes, count): append the marker to unmarked lines whose key is in done_keys. Trailing spaces before
    the marker go; everything else, line endings (CRLF) and undecodable bytes included, stays as it was."""
    lines = _split_lines(raw.decode("utf-8", errors="surrogateescape"))
    n = 0
    for i, s in enumerate(lines):
        body = s.rstrip("\r\n")
        if not body.strip() or C.marker_re.search(body):
            continue
        if inbox_key(body) in done_keys:
            lines[i] = body.rstrip() + C.marker + s[len(body):]
            n += 1
    return "".join(lines).encode("utf-8", errors="surrogateescape"), n


def write_if_unchanged(path, old, new):
    """Replace the file with `new` only if it still holds `old` (somebody may be editing it right now)."""
    path = path.resolve()
    if path.read_bytes() != old:
        return False
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_bytes(new)
    with contextlib.suppress(OSError):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return True


def mark_inbox():
    """Append the marker to inbox lines whose track is done or owned and present in the archive. If the file
    changes between read and write it is left alone (the next loop iteration tries again)."""
    try:
        if not C.inbox.exists():
            return 0
        done = {inbox_key(r["inbox_line"]) for r in read_rows() if r["inbox_line"]
                and r["status"] in ("done", "owned") and r["file"] and (C.archive / r["file"]).exists()}
        if not done:
            return 0
        raw = C.inbox.read_bytes()
        new, n = mark_lines(raw, done)
        if n and write_if_unchanged(C.inbox, raw, new):
            log(f"inbox: marked {n} line(s)")
            return n
    except Exception as e:
        log(f"could not mark the inbox: {e}")
    return 0


def same_track(a, b):
    sa, ca, pa = split_row(a)
    sb, cb, pb = split_row(b)
    if ca != cb or set(pa) != set(pb):
        return False
    wa, wb = {w for g in sa for w in g}, {w for g in sb for w in g}
    return bool(wa & wb) or not (wa or wb)


def cmd_inbox():
    """The inbox items and their state (NEW = not imported into the tracklist yet)."""
    linked = {inbox_key(r["inbox_line"]): r for r in read_rows() if r["inbox_line"]}
    items = read_inbox()
    if not items:
        print(f"{C.inbox}: no items")
    for m in items:
        r = linked.get(inbox_key(m["text"]))
        state = "DOWNLOADED" if m["marked"] else (f"[{r['id']}] {r['status'] or 'pending'} · {r['folder']}" if r
                                                  else "NEW")
        print(f"{m['line']:>3} | heading: {m['heading'] or '-'} | {m['text']} | {state}")


def cmd_add(line, artist, title, length="", playlist="", bpm="", key="", notes=""):
    """Import a resolved inbox item into the tracklist. When the track is already listed no row is added: the
    item is linked to that row and the playlist membership is added (marked at once if it is archived)."""
    name = playlist if playlist in C.folders else next((p for p, f in C.folders.items() if f == playlist), None)
    if not name:
        sys.exit(f"unknown playlist '{playlist}'. Options: {', '.join(C.folders)}")
    if length:
        try:
            parse_length(length)
        except ValueError:
            sys.exit(f"'{length}' is not a length (mm:ss)")
        parts = length.split(":")
        if len(parts) == 2:
            length = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    with lock():
        rows = read_rows()
        k = inbox_key(line)
        if any(inbox_key(r["inbox_line"]) == k for r in rows):
            print("this item is already imported")
            return
        same = next((r for r in rows if same_track(r, {"artist": artist, "title": title})), None)
        if same:
            same["inbox_line"] = line
            if name not in playlists_of(same):
                same["playlists"] = (same["playlists"].strip() + "; " + name).strip("; ")
            print(f"already listed: [{same['id']}] {file_stem(same)} ({same['status'] or 'pending'}); item linked")
        else:
            tid = str(max([int(r["id"]) for r in rows if r["id"].isdigit()], default=0) + 1)
            rows.append({**{a: "" for a in COLUMNS}, "id": tid, "artist": artist, "title": title, "length": length,
                         "bpm": bpm, "key": key, "playlists": name, "folder": C.folders[name], "source": "inbox",
                         "notes": notes, "inbox_line": line})
            print(f"[{tid}] added: {artist} - {title} · "
                  f"{length or 'no length (cluster rule, extra care)'} · {C.folders[name]}")
        write_rows(rows)
    mark_inbox()


def cmd_search(query, artist="", title="", length=""):
    """Resolving helper: one search paced like the loop, printing the length clusters of the audio files found.
    With artist/title the loop's identity matching filters them; with length, the matching cluster is flagged."""
    while True:
        with lock():
            st = load_state()
            wait = C.search_interval - (time.time() - st["last_search"])
            if wait <= 0:
                st["last_search"] = time.time()
                save_state(st)
                break
        time.sleep(min(wait, 10))
    res = search(query)
    row = {"id": "?", "artist": artist, "title": title or query}
    ts = {"candidates": {}}
    if artist or title:
        record_candidates(row, ts, res, time.time())
    else:
        for r in res:
            for f in r.get("files", []):
                ext = pathlib.PurePosixPath(f["filename"].replace("\\", "/")).suffix.lower()
                if ext in AUDIO_EXTS:
                    ts["candidates"][r["username"] + "|" + f["filename"]] = {
                        "user": r["username"], "fn": f["filename"], "ext": ext, "size": f.get("size", 0),
                        "len": f.get("length"), "br": f.get("bitRate")}
    cands = list(ts["candidates"].values())
    target = parse_length(length) if length else None
    print(f"'{query}': {len(res)} users, {len(cands)} matching audio files")
    for k in clusters(cands):
        hit = "  <= target" if target and abs(k["center"] - target) <= tol(target) else ""
        print(f"  {mmss(k['center'])}: {k['sources']} sources, labels {k['labels']}, "
              f"formats {sorted({c['ext'] for c in k['members']})}{hit}")
        for o in sorted({basename(c["fn"]) for c in k["members"]})[:3]:
            print(f"      {o}")
    unknown = [c for c in cands if not cluster_length(c)]
    if unknown:
        print(f"  {len(unknown)} file(s) without a reported length (MP3/FLAC), e.g. "
              f"{', '.join(sorted({basename(c['fn']) for c in unknown})[:3])}")


# ---------------------------------------------------------------- outputs: index, README, rekordbox.xml
def write_index(rows=None):
    """archive-index.csv (per track: folder, file, artist, title, length, BPM, key, format, size, playlists, source,
    note) and README.md (tracks and total length per folder, how to import into Rekordbox) inside the archive."""
    rows = rows if rows is not None else read_rows()
    items = []
    for r in rows:
        if r["status"] == "duplicate" or not r["file"] or not (C.archive / r["file"]).exists():
            continue
        p = C.archive / r["file"]
        items.append({"folder": r["folder"], "file": p.name, "artist": r["artist"], "title": r["title"],
                      "length": r["length"], "bpm": r["bpm"], "key": r["key"], "format": p.suffix.lower().lstrip("."),
                      "size_mb": f"{p.stat().st_size / 1e6:.1f}", "playlists": "; ".join(playlists_of(r)),
                      "source": "inbox" if r["inbox_line"] else ("owned" if r["status"] == "owned" else "tracklist"),
                      "note": r["check"]})
    items.sort(key=lambda k: (k["folder"], k["artist"].lower(), k["title"].lower()))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(items[0].keys()) if items else ["folder"], lineterminator="\n")
    w.writeheader()
    w.writerows(items)
    C.archive.mkdir(parents=True, exist_ok=True)
    (C.archive / "archive-index.csv").write_text(buf.getvalue(), encoding="utf-8")
    per_folder = {}
    for k in items:
        o = per_folder.setdefault(k["folder"], [0, 0])
        o[0] += 1
        o[1] += int(parse_length(k["length"]) or 0)
    pending = sum(1 for r in rows if r["status"] not in ("done", "owned", "duplicate"))
    lines = ["# DJ archive", "", f"Updated {datetime.now():%Y-%m-%d %H:%M} · {len(items)} track{'s' * (len(items) != 1)} · "
             f"{pending} more on the way (the loop collects them in the background).", "",
             "| Folder | Tracks | Total length |", "|---|---|---|"]
    for folder, (n, secs) in sorted(per_folder.items()):
        lines.append(f"| {folder} | {n} | {secs // 3600} h {secs % 3600 // 60} min |")
    lines += ["", "- One file per track; tracks that belong to several playlists are linked through `rekordbox.xml`.",
              "- Full list: `archive-index.csv` (length, BPM, key, format, playlists, source, note).",
              "- Total length counts the tracks with a reference length in the tracklist.",
              "- Import into Rekordbox: Preferences > View > Layout: enable \"rekordbox xml\"; Preferences >",
              "  Advanced > Database > rekordbox xml > Imported Library: choose `rekordbox.xml` in this folder; in the",
              "  left tree, right-click the folders under rekordbox xml > Import Playlist.",
              f"- To request new tracks, add lines to the inbox file; archived ones get \"{C.marker.strip()}\"",
              "  appended.",
              "- This file, the index and the XML are rewritten automatically; manual edits are overwritten."]
    (C.archive / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(items)


def export_rekordbox():
    """<archive>/rekordbox.xml: the playlist tree, one file per track (a `duplicate` row whose check says
    "duplicate:ID" uses the file of row ID). Also rewrites the index and README.md."""
    rows = read_rows()
    id_path = {r["id"]: C.archive / r["file"] for r in rows if r["file"] and (C.archive / r["file"]).exists()}
    for r in rows:
        m = re.match(r"duplicate:\s*([^\s;,)]+)", r["check"])
        if r["status"] == "duplicate" and m and m.group(1) in id_path:
            id_path[r["id"]] = id_path[m.group(1)]
    path_id, tracks = {}, []
    for r in rows:
        p = id_path.get(r["id"])
        if p and p not in path_id:
            path_id[p] = len(path_id) + 1
            tracks.append((path_id[p], r, p))
    root_el = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root_el, "PRODUCT", Name="rekordbox", Version="7.0.0", Company="AlphaTheta")
    col = ET.SubElement(root_el, "COLLECTION", Entries=str(len(tracks)))
    kinds = {".mp3": "MP3 File", ".aiff": "AIFF File", ".aif": "AIFF File", ".flac": "FLAC File",
             ".wav": "WAV File", ".m4a": "M4A File"}
    for tid, r, p in tracks:
        ET.SubElement(col, "TRACK", TrackID=str(tid), Name=r["title"], Artist=r["artist"],
                      Kind=kinds.get(p.suffix.lower(), "MP3 File"), Size=str(p.stat().st_size),
                      Location="file://localhost" + urllib.parse.quote(str(p)))
    playlists = ET.SubElement(root_el, "PLAYLISTS")

    def node(parent, name, children):
        if children is None:
            members = []
            for r in rows:
                p = id_path.get(r["id"])
                if p and name in playlists_of(r) and path_id[p] not in members:
                    members.append(path_id[p])
            n = ET.SubElement(parent, "NODE", Type="1", Name=name, KeyType="0", Entries=str(len(members)))
            for m_ in members:
                ET.SubElement(n, "TRACK", Key=str(m_))
        else:
            n = ET.SubElement(parent, "NODE", Type="0", Name=name, Count=str(len(children)))
            for a, c in children:
                node(n, a, c)

    top = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count=str(len(C.tree)))
    for a, c in C.tree:
        node(top, a, c)
    ET.indent(root_el)
    C.archive.mkdir(parents=True, exist_ok=True)
    out = C.archive / "rekordbox.xml"
    ET.ElementTree(root_el).write(out, encoding="UTF-8", xml_declaration=True)
    write_index(rows)
    print(f"{out}: {len(tracks)} track{'s' * (len(tracks) != 1)}, {len(C.folders)} playlists · "
          "archive-index.csv and README.md updated")


# ---------------------------------------------------------------- CLI
def _raise_interrupt(*_):
    raise KeyboardInterrupt()


def build_parser():
    ap = argparse.ArgumentParser(prog="archivist.py", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config JSON (default: $DJ_ARCHIVIST_CONFIG, else ~/.dj-archivist/config.json)")
    sp = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sp.add_parser("run", help="run the loop in the foreground")
    sp.add_parser("start", help="start the loop as a detached background process")
    sp.add_parser("stop", help="stop the background loop (active transfers continue in slskd)")
    p = sp.add_parser("round", help="search and download the first N open tracks, then exit (trial run)")
    p.add_argument("--limit", type=int, default=5)
    sp.add_parser("status", help="status counts, active downloads, recent events")
    sp.add_parser("decisions", help="tracks waiting for a decision, with the evidence (length clusters)")
    p = sp.add_parser("decide", help="decide a track: target length, keep waiting, or skip")
    p.add_argument("id")
    p.add_argument("value", metavar="mm:ss|wait|skip")
    sp.add_parser("inbox", help="inbox items and their state (NEW = not imported yet)")
    p = sp.add_parser("add", help="import a resolved inbox item into the tracklist")
    p.add_argument("--line", required=True, help="the inbox line, as written")
    p.add_argument("--artist", required=True)
    p.add_argument("--title", required=True, help="with the version name, e.g. 'Track (Extended Mix)'")
    p.add_argument("--length", default="", help="length of the chosen version, mm:ss")
    p.add_argument("--playlist", required=True, help="playlist name (or its archive folder)")
    p.add_argument("--bpm", default="")
    p.add_argument("--key", default="")
    p.add_argument("--notes", default="", help="context: inbox heading, where it was heard, genre")
    sp.add_parser("mark", help="append the inbox marker to lines whose tracks are archived")
    p = sp.add_parser("search", help="one paced search that prints length clusters (resolving helper)")
    p.add_argument("query")
    p.add_argument("--artist", default="")
    p.add_argument("--title", default="")
    p.add_argument("--length", default="", help="flag the cluster of this length (mm:ss)")
    p = sp.add_parser("import-local", help="file owned tracks from local_import_dir and orphans from incoming/")
    p.add_argument("--dry-run", action="store_true", help="only show what would be filed")
    p.add_argument("--id", nargs="*", help="only these track ids")
    sp.add_parser("rekordbox", help="write rekordbox.xml, archive-index.csv and README.md into the archive")
    sp.add_parser("retag", help="retag archive files whose artist/title differ from the tracklist or carry spam")
    return ap


def main(argv=None):
    signal.signal(signal.SIGTERM, _raise_interrupt)
    a = build_parser().parse_args(argv)
    load_config(a.config)
    commands = {
        "run": lambda: run_loop(),
        "start": cmd_start,
        "stop": cmd_stop,
        "round": lambda: run_loop(limit=a.limit),
        "status": cmd_status,
        "decisions": cmd_decisions,
        "decide": lambda: cmd_decide(a.id, a.value),
        "inbox": cmd_inbox,
        "add": lambda: cmd_add(a.line, a.artist, a.title, a.length, a.playlist, a.bpm, a.key, a.notes),
        "mark": mark_inbox,
        "search": lambda: cmd_search(a.query, a.artist, a.title, a.length),
        "import-local": lambda: cmd_import_local(a.dry_run, set(a.id or [])),
        "rekordbox": export_rekordbox,
        "retag": cmd_retag,
    }
    try:
        commands[a.command]()
    except KeyboardInterrupt:
        log(f"{a.command} stopped")


if __name__ == "__main__":
    main()
