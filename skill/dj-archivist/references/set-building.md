# Set Building Method

## 1. Intake

Collect (ask only for what's missing):

- Track pool: title, artist, BPM, Camelot key (note the source: DJ software analysis, filename or
  the DJ's ear), vocal density (none / sparse / heavy), energy 1 to 10.
- Context: focus set (plateau, e.g. music to work or code to) or club/party set (arc)? Target length?
- Constraints: must-include tracks, tracks already used in previous sets of a series.

## 2. Structure archetypes

**Plateau (default for focus sets).** BPM stays in a ~5 BPM band with keylock.
Energy varies subtly through texture and harmonic movement, not tempo. Shape:
settle-in (2 to 3 tracks), then a hypnotic core (long harmonic run, ideally a sustained
single-key or adjacent-key stretch, e.g. a 1A run), then a peak run (the set's densest
material, e.g. a closing artist block), then a single-track landing.

**Hump.** Apex around 60% of the set (e.g. track 8 of 13), symmetric-ish
rise and release. Good middle ground when the user wants some drama without a
club arc.

**Build-to-end arc.** Classic club shape, energy peaks at the close. Use only when
the set is for a live/party context or the user explicitly chooses it.

When the choice is open, present hump vs. arc (or plateau) as an explicit decision
with one sentence of trade-off each; don't silently pick.

## 3. Harmonic sequencing (Camelot)

Allowed moves, in order of preference for hypnotic texture:

1. Same key (1A to 1A): strongest glue, backbone of the hypnotic core.
2. ±1 same letter (1A to 2A or 12A): energy nudge without tension.
3. Letter swap (1A to 1B): brightness shift; use to mark section boundaries.
4. Diagonal (8A to 9B): smooth, shares six of seven notes; a subtle lift.
5. "Energy boost" moves, +2 (8A to 10A) or +7 (8A to 3A): audible tension, use sparingly,
   at most 1 to 2 per set, at the apex or into the peak run.

Rules of thumb:

- Harmonic isolation is a cut reason: a track whose key connects to nothing adjacent
  in the pool forces hard cuts; drop it or find a bridge track.
- Long blends (16 to 32 bars) require compatibility for the *whole* overlap: check that
  both tracks' active sections during the blend are harmonically stable (breakdowns
  with pads are the danger zone).
- Key metadata conflicts (filename vs software analysis) go on the pending list; suggest the
  user verify by ear against a reference track in the claimed key.

## 4. BPM handling

- Focus sets: pick the plateau band from where the pool clusters (house and tech house
  usually sit around 124 to 133). Keylock on. Tracks outside the band by more than 2 BPM get
  stretched only if keylock artifacts are acceptable for that genre; UK garage shuffle tolerates about ±3%.
- Order within the band so consecutive jumps are at most 2 BPM; save the largest jump for
  a letter-swap boundary where the ear expects change anyway.

## 5. Vocal density placement

- Heavy-vocal tracks: cut from focus sets, or place at the opener (listener not yet
  in flow) or immediately after the apex (natural release moment).
- Sparse vocal chops/one-liners: fine anywhere; they read as texture.
- Two heavy-vocal tracks back-to-back: never in a focus set.

## 6. Mixing sheet format

Deliver as a table, one row per track:

| # | Track, Artist | BPM | Key | Energy | Vocals | Transition in (bars, technique) | Notes |

Follow the table with:

- **Transition plan**: for each pair, one line: where to start the blend (cue point
  description), how many bars, which technique (see transitions reference), which EQ
  moves.
- **Pending decisions**: numbered list of open items (key conflicts, repeats across
  sets, structure choice). A set is "planned", not "final", until this list is empty.

## 7. Publishing a set (when requested)

- 3 to 5 title options that match the series' identity (for a focus series: flow-state,
  "music to work to"; avoid generic "study music" framing unless that is the identity).
- Copy-paste description: hook line, timestamped tracklist with `00:00` placeholders
  the user fills after recording, artist shoutouts (tag recurring artists), hashtags.
- Series continuity: reference the previous set number; note any track repeated from an
  earlier set in the series.
