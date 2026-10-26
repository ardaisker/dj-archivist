#!/usr/bin/env python3
"""Add archived tracks straight into the Rekordbox library and the DJ's own playlists (no XML import).

Rekordbox 6/7 keeps its library in an encrypted SQLite file (master.db). This module opens it with pyrekordbox
and, for every audio file in the archive:
- adds the file to the collection if it is not there yet (title, artist, BPM and length from the tracklist);
- adds it to the playlist that matches its archive folder, if it is not already a member;
- re-links an existing entry whose file moved to another archive folder (same file name), so cues, analysis and
  playlist memberships are kept instead of creating a duplicate.

Safety rules:
- Never writes while Rekordbox is running (writing to an open library can corrupt it). The archivist loop retries
  every few minutes, so new tracks appear after Rekordbox is closed.
- Copies master.db to a backup folder before every write (the last 10 copies are kept).
- Only adds. Nothing is removed from the DJ's playlists and their order is not touched.
- Analysis (beat grid, waveform, key) stays Rekordbox's job: new tracks arrive unanalysed.

Requires: pip install pyrekordbox sqlcipher3-wheels (tested with pyrekordbox 0.5.0, Rekordbox 7.2).

Standalone: python3 rekordbox_library.py ARCHIVE_DIR [--parent FOLDER] [--map "Archive folder=Playlist" ...]
[--dry-run]
"""
import pathlib
import shutil
import subprocess
import sys
import warnings
from datetime import datetime

AUDIO = {".mp3", ".aiff", ".aif", ".flac", ".wav", ".m4a"}
REKORDBOX_DIR = pathlib.Path.home() / "Library" / "Pioneer" / "rekordbox"      # macOS
KEEP_BACKUPS = 10


def rekordbox_running():
    for name in ("rekordbox", "rekordbox.exe"):
        cmd = ["pgrep", "-x", name] if sys.platform != "win32" else ["tasklist", "/FI", f"IMAGENAME eq {name}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if (r.returncode == 0 and sys.platform != "win32") or (sys.platform == "win32" and name in r.stdout):
            return True
    return False


def plan(files, library, members, playlist_for):
    """Pure planning step (tested without Rekordbox).
    files: {path: folder name} of archive audio files. library: set of paths already in the collection.
    members: {playlist name: set of paths}. playlist_for: {folder name: playlist name}.
    Returns (add, relink, link): paths to add to the collection, (old path, new path) pairs to re-link, and
    (path, playlist) pairs to add to a playlist."""
    archive_paths = set(files)
    missing = {pathlib.Path(p).name: p for p in library if p not in archive_paths}
    add, relink, link = [], [], []
    for path in sorted(files):
        known = path
        if path not in library:
            old = missing.pop(pathlib.Path(path).name, None)
            if old is not None and pathlib.Path(old).parent != pathlib.Path(path).parent:
                relink.append((old, path))
                known = old
            else:
                add.append(path)
        pl = playlist_for.get(files[path])
        if pl and path not in members.get(pl, set()) and known not in members.get(pl, set()):
            link.append((path, pl))
    return add, relink, link


def backup(backup_dir):
    backup_dir = pathlib.Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"master-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(REKORDBOX_DIR / "master.db", target)
    for old in sorted(backup_dir.glob("master-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    return target


def sync(archive, playlist_for, parent="", info=None, backup_dir=None, dry_run=False):
    """archive: archive root. playlist_for: {archive folder relative to the root ("House", "Sets/Warm-up"):
    Rekordbox playlist name}.
    parent: the Rekordbox folder that holds those playlists ("" = anywhere, first match by name).
    info: {path: {"artist", "title", "bpm", "length"}} from the tracklist.
    Returns a one-line summary, or None when Rekordbox is running (nothing written; try again later)."""
    if rekordbox_running():
        return None
    try:
        warnings.simplefilter("ignore")
        from pyrekordbox.masterdb import MasterDatabase
    except ImportError:
        raise RuntimeError("pyrekordbox is not installed: pip install pyrekordbox sqlcipher3-wheels")
    archive = pathlib.Path(archive).expanduser()
    info = info or {}
    files = {str(p): p.parent.relative_to(archive).as_posix() for p in archive.rglob("*")
             if p.suffix.lower() in AUDIO and p.parent != archive}
    db = MasterDatabase()
    try:
        content = {c.FolderPath: c for c in db.get_content().all()}
        in_archive = {p for p in content if p.startswith(str(archive) + "/")}
        candidates = [p for p in db.get_playlist().all() if p.Attribute == 0
                      and (not parent or (p.Parent is not None and p.Parent.Name == parent))]
        playlists, not_found = {}, []
        for folder, name in playlist_for.items():
            pl = next((p for p in candidates if p.Name.lower() == name.lower()), None)
            if pl is None:
                not_found.append(name)
            else:
                playlists[name] = pl
        members = {name: {s.Content.FolderPath for s in pl.Songs if s.Content is not None}
                   for name, pl in playlists.items()}
        add, relink, link = plan(files, in_archive, members,
                                 {f: n for f, n in playlist_for.items() if n in playlists})
        if not dry_run and (add or relink or link):
            if backup_dir:
                backup(backup_dir)
            for old, new in relink:
                c = content[old]
                c.FolderPath, c.FileNameL = new, pathlib.Path(new).name
                content[new] = c
            for path in add:
                content[path] = _add_track(db, path, info.get(path, {}))
            for path, name in link:
                db.add_to_playlist(playlists[name], content[path])
            db.commit()
        summary = (f"rekordbox: {len(add)} new track{'s' * (len(add) != 1)}, {len(relink)} re-linked, "
                   f"{len(link)} playlist addition{'s' * (len(link) != 1)}")
        if not_found:
            summary += f" · playlist not found: {', '.join(not_found)}" + (f" (under {parent})" if parent else "")
        return summary + (" (dry run)" if dry_run else "")
    finally:
        db.close()


def _add_track(db, path, meta):
    extra = {"Title": meta.get("title") or pathlib.Path(path).stem}
    try:
        if meta.get("bpm"):
            extra["BPM"] = int(round(float(meta["bpm"]) * 100))
    except ValueError:
        pass
    length = meta.get("length") or ""
    if ":" in length:
        m, s = length.split(":")[-2:]
        extra["Length"] = int(m) * 60 + int(s)
    c = db.add_content(path, **extra)
    if meta.get("artist"):
        a = db.get_artist(Name=meta["artist"]).first() or db.add_artist(name=meta["artist"])
        c.ArtistID = a.ID
    return c


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--parent", default="", help="Rekordbox folder that holds the playlists")
    ap.add_argument("--map", nargs="*", default=[], help='"Archive folder=Playlist name" (default: same name)')
    ap.add_argument("--backup-dir", default="~/.dj-archivist/rekordbox-backups")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path(a.archive).expanduser()
    mapping = {d.name: d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")}
    mapping.update(dict(m.split("=", 1) for m in a.map))
    result = sync(root, mapping, a.parent, backup_dir=pathlib.Path(a.backup_dir).expanduser(), dry_run=a.dry_run)
    print(result if result is not None else "Rekordbox is running: close it and run again")
