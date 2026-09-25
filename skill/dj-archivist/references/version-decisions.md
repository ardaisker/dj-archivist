# Version decisions

The loop settles clear cases on its own. You are called for the tracks it parks as
`decision`. `python3 scripts/archivist.py decisions` prints, for each one: the reference
length, BPM and key (if known) and every length cluster seen on the network, with source
count, version labels, formats and sample filenames.

## The evidence, strongest first

1. **Reference length.** The version the DJ actually played, from an old library, an
   export or a store listing. A file within ±3 s is the same master. If it exists anywhere
   on the network, the answer is to wait for it (`decide ID wait`), not to settle for another
   version, unless the DJ says otherwise.
2. **Cross-source consistency.** Independent users (different usernames, different folder
   trees) holding a file of the same length is strong evidence that the length is a real
   release, not a bad rip or somebody's private edit. One source is weak; three or more is
   solid.
3. **Metadata that matches the official release.** Key and BPM prefixes in filenames (for
   example "1A - 130 - Artist - Title (Extended Mix)") that agree with the store's key and
   tempo point to files taken from the official release or a DJ pool.
4. **Labels.** "Extended Mix", "Original Mix", "Club Mix", "Extended Edit" and "Dub" name
   long versions; "Radio Edit", "Radio Mix", "Short" and "Clean Edit" usually name short
   ones. Labels are hints; a label on at least one member of a consistent cluster confirms
   that cluster.
5. **Musical plausibility.** Bars = length × BPM / 240. A mixable house or tech house version
   carries 16 to 32 bars of beat-only intro and outro, so at 124 to 132 BPM it usually runs
   5:00 to 7:30 (about 160 to 250 bars). Radio edits run 2:30 to 3:45 (about 80 to 120 bars).
   Bootleg edits often run 3:00 to 4:30 and are still mixable; for those, the DJ's own
   reference matters more than any rule of thumb.

## Decision patterns

- **Reference missing, a consistent labelled long version within about 15 s of it:** most
  likely the same release with different padding or a remaster. Accept that length.
- **Reference missing, only a much shorter version on the network:** never accept a radio
  edit as a substitute for a mixable version. Wait; after the last round it becomes
  `skip` (buy or rip it yourself).
- **Reference missing, a clearly longer version on the network** (for example 6:10
  extended where the old file was 4:02): a different version. Offer it to the DJ as a
  choice; do not decide alone. Default: wait.
- **No reference, two labelled clusters with different labels** (Extended 6:12 vs
  Original 5:40): take the one whose label matches the title; if the title has no label,
  take the one with more independent sources, and ask when they are close.
- **No reference, two clusters with the same label** (two different "Extended Mix"
  lengths): weigh source count, metadata that matches the official release, and any store
  or catalog record. Pick one, and add a "listen" note so the DJ checks it.
- **No reference, one source only:** acceptable if the label is explicit (Extended,
  Original, Club) and the length is plausible for the BPM; otherwise wait.
- **Clean vs dirty:** identical length, so length cannot decide. The title says which one
  the DJ wants; the loop already prefers it and notes a mismatch.

## Search is loose, matching is strict

The loop searches each round with name variants first, then keyword combinations, when the
full name does not surface the wanted file. Any file found that way still has to pass the
same identity and length checks. Never loosen matching to rescue a track; decide it here
instead.

## After deciding

- Record every decision with `decide`, never by editing the state file by hand.
- Keep a short "for the DJ" list: substitutions you accepted, tracks you skipped (to buy),
  and files flagged "listen" (possible upsampled 320s, same-label ambiguities).
- When a batch has landed, run `rekordbox` and remind the DJ to re-import the XML.
