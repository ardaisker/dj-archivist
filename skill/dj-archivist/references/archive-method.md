# Archive method

How the Archive Builder turns a list of tracks into a verified, mix-ready library. The
pipeline (`scripts/archivist.py`) implements every rule below; thresholds live in the
config file. Change a rule here first, then in the config or code.

## 1. The tracklist

One CSV row per track, with the columns `id, artist, title, length, bpm, key, playlists,
folder, status, file, check, notes, source, inbox_line`: the title carries the version name
the DJ wants, `length` is the reference length, `playlists` lists every playlist the track
belongs to (separated by `;`), and `file` is filled in once it lands. Status values:
`pending` (empty), `waiting`, `candidate`, `downloading`, `done`, `owned`, `duplicate`,
`decision`, `not_found`.
Good sources for building it:

- **Screenshots or photos of an old library.** DJ software shows Title, Artist, BPM, Time
  and Key columns; the Time column is the most valuable field you can extract, because it
  identifies the exact version the DJ used. Cross-check readings: the same track in two
  playlists must show the same values, and playlist totals (shown at the bottom of many
  library views) must add up.
- **Playlist exports** (Rekordbox, Serato, Traktor, streaming services).
- **The inbox file** (section 7).

Duplicates happen (the same track in two playlists, spelling variants). Keep one row per
track; give the others status `duplicate` and `duplicate:<id>` in the `check` column. Their
playlist memberships still count: the Rekordbox export points them at the original's file.

## 2. The right version: length decides

A DJ needs the mixable version: usually the Extended Mix or Original Mix, with beat-only
intro and outro sections of 16 to 32 bars. Labels are unreliable: the same "Extended Mix"
label appears on different edits, and DJ-pool versions (Intro, Clean, Dirty) share names
with store releases. The marker that does not lie is the length.

- **Reference length** (from the old library, an export, or a store listing): a file within
  max(3 s, 1%) of it is the same master. Anything else is another version, whatever its name.
- **No reference:** cluster the candidate files by length. A cluster found at two or more
  independent sources (different users), with an explicit version label (extended,
  original, club; for loosely named inbox items only these three count) on at least one
  file, is the right length. A radio-labelled cluster never counts unless the title asks for
  a radio edit. If the title names the version, the cluster carrying that label wins.
- **Undecided:** two plausible clusters, a single source, or a reference length that is not
  on the network while another version is. The track is parked as `decision`; resolve it
  with `references/version-decisions.md`.
- **Someone else's remix is never a candidate:** a bracket in the filename that says
  remix/edit/mix and carries a name absent from the title and artist.
- **Clean and dirty** versions have the same length. Prefer the one the title asks for; if
  only the other exists, take it and add a note.

## 3. Format policy

Default order (configurable in `format_priority`):

1. MP3, only at 320 kbps CBR.
2. Lossless: AIFF, then FLAC, then WAV.
3. M4A/AAC at 256 kbps or more.

MP3 below 320 kbps and VBR MP3 are never taken. Search results often omit length and
bitrate, so the pipeline estimates them from the file size before downloading:

- MP3 320 is about 40 KB per second plus up to about 4 MB of tags and artwork.
- 16-bit/44.1 kHz PCM (WAV/AIFF) is 176.4 KB per second. 24-bit and 48 kHz are also
  checked.
- FLAC compresses 35 to 85%, so it only gets a coarse check.

That rules out wrong versions and low bitrates before a single byte is transferred. After
download, ffprobe verifies codec, bitrate and length. A file that fails goes to the
`rejected/` folder (never deleted) and the next candidate is tried.

**Upsampled files** (a 128 kbps source re-encoded as 320) lack energy above roughly
16 to 17 kHz. The pipeline compares the 17.5 to 19.5 kHz band with the 12 to 15 kHz band
and adds a "listen" note when the high band is unusually weak. The check catches 128 kbps
sources but cannot separate 192 kbps sources from genuine 320s, so it never rejects. The
DJ's ears decide.

## 4. Sources and download tracking

Sources, in the order you should prefer them: the DJ's own files and backups
(`import-local`), stores for anything worth owning, and (optional) the Soulseek network
through slskd for material the DJ is entitled to obtain.

A download is never guaranteed. Peers set their own rules: some refuse users who share
nothing, some have quotas, queues or go offline. Every transfer is tracked:

- **Rejected, errored, timed out:** try the next valid candidate (same version and format
  rules).
- **"Banned":** that user is never tried again.
- **"Too many megabytes/files":** that user is skipped for 24 hours.
- **3 rejections in 7 days:** the user is skipped for a week.
- **Remote queue:**
  - Waiting longer than `remote_queue_timeout` (default 2 h): the candidate is released and
    may be retried after 24 hours.
  - A known place far down the queue (no free slot, a queue longer than
    2 × `reachable_queue`): released after 10 minutes.
- **Stuck:** a transfer that never starts (15 min) or stops progressing (20 min) is
  cancelled.
- **Concurrency:** at most `max_active` files (default 2) actually transfer at once.
  Remote-queued requests do not use bandwidth, so they do not count toward that limit.
  They do count toward `max_open_requests` (default 6). One request per user at a time;
  30 s between enqueues.

Ranking puts reachability before format: a peer with a free slot or a short queue beats a
better format stuck behind thousands of queued requests. Then comes format priority, then
clean/dirty match, strength of the length evidence, freshness, the peer's history, free
slot, queue length and speed.

## 5. Searching: repeated and patient

The same search on Soulseek can return results one hour and nothing the next. Each round:

1. **Name variants:**
   - first artist + title + remixer
   - first artist + title
   - remixer + title
   - title alone

   Never put "-", brackets, keys or BPMs in a query. Some big artist names are blocked by
   the server; search the remixer's name instead.
2. **Keyword combinations,** when the name variants do not surface the wanted file.
   Candidates are ranked by distinctiveness; the owner of the version (the row's artist and
   the remixer) comes before the original artist, and long or numeric words go first.
   - Build queries in this order: owner + title word, owner + two title words, title
     triples and pairs, single distinctive words.
   - Common words are only used next to a distinctive one; two-letter words are skipped.
   - At most `keyword_queries_per_round` (default 8) per round; each round takes the next
     slice.

**Pacing:**
- One search every `search_interval` seconds (default 60).
- After 3 empty results in a row, a canary search for a common term tells real silence
  from server throttling. If the canary is empty too, searching pauses for 15 minutes and
  those empty rounds do not count.

**Backoff:** a round with no usable file schedules the next one after 2 h, 8 h, 1 day, 3 days
and 7 days. After the last round the track becomes `not_found` and goes on the buy or
manual list.

Evidence accumulates: every file seen is remembered, so length clusters get stronger over
time. There is no target speed. A first pass over a few hundred tracks takes hours; the
long tail takes days.

## 6. The archive

- **Folders mirror the DJ's playlist tree** (`playlist_tree`, `playlist_folders`).
- **One file per track,** in the first genre playlist it belongs to (a set folder only if
  it belongs to nothing else). The CSV `folder` column can override this.
- **File names:** `Artist - Title.ext` as written in the tracklist.
- **Tags:** artist and title come from the tracklist; tags containing URLs or shop spam
  are blanked. Audio is never re-encoded: tags are written with a stream copy and the
  audio packet checksum must match, otherwise the file is copied untagged.
- **Kept current automatically,** whenever the file count changes:
  - `rekordbox.xml`: the full playlist tree, one file per track, every membership;
  - `archive-index.csv`: every track with folder, length, BPM, key, format, size,
    playlists, source and notes;
  - a generated `README.md` with counts and total time per folder.
- **Importing into Rekordbox:**
  1. Preferences > View > Layout: enable "rekordbox xml".
  2. Preferences > Advanced > Database > rekordbox xml > Imported Library: select the
     file.
  3. In the tree, right-click the folders under rekordbox xml > Import Playlist.

## 7. The inbox file

A plain text file where the DJ writes tracks they want, as loose as they like. Lines ending
with ":" (and without " - "), or starting with "#", are headings that give context to the
items below them (a genre, where the track was heard). Bullets and numbering are allowed.

For each new item, the assistant (not the loop) does the careful part:

1. Resolve the real release on the web: official credit (collaborations are common),
   label, available mixes, BPM, key, genre.
2. Pick the mixable version (Extended, else Original, else the edit itself for a bootleg;
   never a radio or "short" edit) and find its length. Open catalogs such as the Deezer and
   Apple search APIs return durations; stores often list only the radio edit, in which case
   Soulseek evidence (a labelled cluster with key/BPM tags matching the official metadata)
   can settle it.
3. Probe availability with `archivist.py search`.
4. Choose the most related existing crate from the heading, the track's genre and what
   each crate means to the DJ. Do not invent new folders.
5. `archivist.py add --line "<the item as written>" --artist ... --title "... (Extended Mix)"
   --length mm:ss --playlist ...`. An item that matches a track already in the tracklist is
   linked to it instead of creating a new row.

Inbox items get priority in searching and downloading. When a track lands, the loop
appends " - DOWNLOADED" to its line. The rest of the file stays byte-for-byte unchanged,
and nothing is written if the file changed while it was being read (the DJ may be editing
it). Removing marked lines is the DJ's call.

## 8. Sharing

Many peers refuse users who share nothing. The only lawful fix is to share content that
may be redistributed: Creative Commons or public-domain music with its license files,
or the DJ's own productions. Keep the share folder (`<work_dir>/share` by default) separate
from the archive, and never add its files to the tracklist or the Rekordbox export. Never share
purchased or downloaded commercial music.

## 9. Who does what

- **The loop (`archivist.py run`, in the background):** search, version choice, download,
  tracking, verification, tagging, filing, marking the inbox, XML and index. It never makes
  judgement calls; ambiguity goes to `decision`.
- **The assistant (scheduled daily, or on request):** keep the loop alive, resolve inbox
  items, decide parked versions, import local files, report.
- **The DJ:** listen to flagged files, buy what is missing, import the XML, write the inbox.
