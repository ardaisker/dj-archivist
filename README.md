# DJ Archivist

An AI skill for DJs. It builds and maintains a clean, mix-ready track archive, plans sets,
coaches mixing, and knows its way around electronic music.

> **Disclaimer.** This project is provided as is, for educational and personal archival
> use. It does not host, index or distribute music. You alone are responsible for how you
> use it and for complying with copyright law and the terms of any network or service you
> connect it to. The author accepts no liability for any use or misuse. Read the full
> [DISCLAIMER](DISCLAIMER.md) before using it.

## Why

A DJ library breaks in boring ways:
- the laptop dies;
- files come without backups;
- the "Extended Mix" you grabbed turns out to be a different edit;
- a 128 kbps rip sneaks in as a 320;
- tags are full of shop spam;
- the same track sits in five folders with five sets of cue points.

DJ Archivist is a set of instructions, references and scripts that lets an AI assistant
(built for [Claude Agent Skills](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills))
fix that patiently and carefully, the way a meticulous record-shop clerk would.

## What it does

**Archive Builder**
- Turns old library screenshots, playlist exports or loose notes into a tracklist
  (artist, title, version, length, BPM, key, playlists).
- **Length is the version.** It identifies the exact version you played by its length,
  not by what the filename claims. With no reference it trusts a length only when
  independent sources agree and at least one names the version; anything ambiguous is
  parked for a decision instead of guessed.
- Format policy: MP3 only at 320 kbps CBR, otherwise lossless (AIFF, FLAC, WAV). Size-based
  estimates rule out wrong versions and low bitrates before downloading; ffprobe verifies
  every file after. Suspected upsampled files get a "listen" note.
- Sources: your own files and backups, store links for what you should buy, and an
  optional Soulseek backend through [slskd](https://github.com/slskd/slskd) for material
  you are entitled to obtain. The backend is paced, respects bans and quotas, tracks every
  transfer and falls back to the next valid file when a peer refuses.
- One file per track, filed into your playlist tree; clean artist/title tags written with a
  stream copy (audio untouched, checksum verified).
- A Rekordbox XML with your whole playlist tree, an archive index CSV and a generated README,
  all kept current automatically.
- An **inbox file**: write loose track names under headings like `House:` or `Heard at
  that party:`. The assistant resolves the real release and the mixable version, then
  adds it. When the track lands, ` - DOWNLOADED` is appended to its line, so you can follow
  progress without opening any tool.

**Set Architect.** Plateau, hump or arc structures; Camelot sequencing; BPM bands; vocal
placement; a mixing sheet with a transition plan per pair.

**Mixing Coach.** A teaching ladder from EQ swaps to drop swaps, with exact controller
steps (examples for the Pioneer DDJ-FLX4), drills and self-checks.

**Genre and Theory Guide.**
- Genres from house and UK garage to techno, trance and drum and bass: BPM ranges,
  arrangement and what mixes with what.
- Version names and what each means for a DJ.
- Phrasing, the Camelot wheel, keylock, EQ bands, gain staging, loudness and file quality.

**Set Renderer.** A finished mix plus a photo or video loop becomes a `.mov` with
bit-for-bit lossless PCM audio, ready for upload.

## Repository layout

```
skill/dj-archivist/
  SKILL.md                       the skill: modes, principles, routines
  references/
    archive-method.md            the archive rules in full
    version-decisions.md         how to decide between versions
    set-building.md              set planning method and mixing sheet format
    transitions-and-effects.md   coaching ladder and effect taste rules
    edm-genres.md                genre field guide and version names
    music-theory-for-djs.md      phrasing, keys, EQ, loudness, file quality
  scripts/
    archivist.py                 the archive loop and CLI
    slskd_setup.py               optional slskd (Soulseek) setup helper
    render_set.py                lossless set-to-video renderer
  templates/                     example config, tracklist and inbox
tests/                           offline tests for the archive logic
```

## Requirements

- An assistant that supports Agent Skills (Claude Code, or the Claude apps with skills
  enabled).
- Python 3.9 or newer (standard library only).
- `ffmpeg` and `ffprobe`.
- Optional: [slskd](https://github.com/slskd/slskd) and a Soulseek account, if you choose to
  use that backend.

## Install

**Claude Code:** copy the skill folder into your skills directory:

```bash
cp -R skill/dj-archivist ~/.claude/skills/
```

**Claude apps:** zip the `skill/dj-archivist` folder and upload it under
Settings > Capabilities > Skills.

## Quick start

1. Copy `templates/config.example.json` to `~/.dj-archivist/config.json` and set:
   - your archive folder, working folder and inbox file;
   - your playlist tree;
   - your format priority.
2. Build a tracklist CSV from `templates/tracklist.example.csv`. Or ask the assistant to
   build it from screenshots of your old library; the Time column is the most valuable
   field.
3. Bring in what you already own:

   ```bash
   python3 skill/dj-archivist/scripts/archivist.py import-local --dry-run
   ```

4. Optional backend: `python3 skill/dj-archivist/scripts/slskd_setup.py` (you type your
   credentials; they are stored only in your local slskd config).
5. Start the loop:

   ```bash
   python3 skill/dj-archivist/scripts/archivist.py start
   python3 skill/dj-archivist/scripts/archivist.py status
   ```

6. Ask the assistant for a daily routine: it keeps the loop alive, resolves your inbox,
   decides parked versions and reports what landed.
7. Import `rekordbox.xml` from your archive folder into Rekordbox (steps in the generated
   README inside the archive).

Run the tests with `python3 tests/test_core.py`.

## Responsible use

- **Buy the music you play.** Artists and labels live on it. Beatport, Bandcamp,
  Traxsource, Juno and label shops are one search away, and the assistant will point you
  there for anything you do not already own.
- **Only obtain files you have the right to:** your purchases and promos, your own
  backups, your own productions, and works licensed for redistribution.
- **Share only content that may be redistributed** (for example Creative Commons music with
  its license). Never share purchased or downloaded commercial music. In several countries,
  sharing on peer-to-peer networks draws warning letters and fines.
- **Be a good peer.** The loop is deliberately slow: it paces searches, limits concurrent
  transfers and never retries a peer that refused you.

## License and disclaimer

Code and documentation are released under the [MIT License](LICENSE). Use of this project is
subject to the [DISCLAIMER](DISCLAIMER.md): you are solely responsible for your use of it,
and the author accepts no responsibility or liability of any kind.
