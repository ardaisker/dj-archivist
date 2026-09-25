#!/usr/bin/env python3
"""Render a DJ set to a YouTube-ready .mov with bit-for-bit lossless PCM audio.

Inputs: one continuous mix recording (WAV/AIFF/FLAC) + one visual (photo or video loop).
The audio stream is stored as PCM matched to the source bit depth ; never re-encoded
lossily. Video is H.264 by default (still image or seamless loop), ProRes optional.

Usage:
    python render_set.py --audio mix.wav --visual cover.jpg --output set.mov
    python render_set.py --audio mix.aiff --visual loop.mp4 --output set.mov --prores
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp", ".heic"}

# Map ffprobe sample_fmt -> lossless PCM codec that preserves it exactly.
SAMPLE_FMT_TO_PCM = {
    "s16": "pcm_s16le", "s16p": "pcm_s16le",
    "s32": "pcm_s32le", "s32p": "pcm_s32le",
    "flt": "pcm_f32le", "fltp": "pcm_f32le",
    "dbl": "pcm_f64le", "dblp": "pcm_f64le",
}


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def ffprobe(path):
    out = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]).stdout
    return json.loads(out)


def pick_pcm_codec(audio_path):
    """Match PCM codec to source so no re-quantization happens."""
    info = ffprobe(audio_path)
    stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
    duration = float(info["format"]["duration"])
    codec = stream.get("codec_name", "")
    sample_fmt = stream.get("sample_fmt", "")
    bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)

    if bits == 24 or codec in ("pcm_s24le", "pcm_s24be"):
        pcm = "pcm_s24le"
    elif sample_fmt in SAMPLE_FMT_TO_PCM:
        pcm = SAMPLE_FMT_TO_PCM[sample_fmt]
        # s32 container often carries 24-bit content; trust bits_per_raw_sample.
        if pcm == "pcm_s32le" and bits == 24:
            pcm = "pcm_s24le"
    else:
        pcm = "pcm_s24le"  # safe upcast: no precision loss going up
    return pcm, duration, stream.get("sample_rate", "?"), bits or sample_fmt


def build_cmd(audio, visual, output, pcm_codec, prores, fps, duration):
    is_image = visual.suffix.lower() in IMAGE_EXTS
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

    if is_image:
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(visual)]
    else:
        cmd += ["-stream_loop", "-1", "-i", str(visual)]

    cmd += ["-i", str(audio)]

    if prores:
        vcodec = ["-c:v", "prores_ks", "-profile:v", "3"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                  "-pix_fmt", "yuv420p"]
        if is_image:
            vcodec += ["-tune", "stillimage"]

    cmd += vcodec
    # Scale to even dimensions (x264 requirement), cap at 4K height.
    cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    cmd += ["-c:a", pcm_codec]          # THE point: lossless PCM, never AAC.
    # Pin output length to the audio exactly; -shortest alone can overshoot
    # by a few seconds due to muxer interleaving with looped inputs.
    cmd += ["-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart"]
    cmd += [str(output)]
    return cmd


def verify(output, expected_duration):
    info = ffprobe(output)
    astream = next(s for s in info["streams"] if s["codec_type"] == "audio")
    codec = astream["codec_name"]
    duration = float(info["format"]["duration"])
    ok_codec = codec.startswith("pcm_")
    ok_dur = abs(duration - expected_duration) <= 0.5
    return ok_codec, ok_dur, codec, duration


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--audio", required=True, type=Path)
    p.add_argument("--visual", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--prores", action="store_true",
                   help="ProRes 422 HQ video instead of H.264 (huge files)")
    p.add_argument("--fps", type=int, default=25,
                   help="Framerate for still-image video (default 25)")
    args = p.parse_args()

    for f in (args.audio, args.visual):
        if not f.exists():
            sys.exit(f"ERROR: input not found: {f}")
    if args.output.suffix.lower() != ".mov":
        sys.exit("ERROR: output must be a .mov (QuickTime container carries PCM cleanly)")

    pcm_codec, duration, rate, depth = pick_pcm_codec(args.audio)
    mins = duration / 60
    print(f"Audio: {args.audio.name} ; {mins:.1f} min, {rate} Hz, depth {depth} -> {pcm_codec}")

    cmd = build_cmd(args.audio, args.visual, args.output, pcm_codec,
                    args.prores, args.fps, duration)
    print("Rendering (long sets take a while)...")
    subprocess.run(cmd, check=True)

    ok_codec, ok_dur, codec, out_dur = verify(args.output, duration)
    size_mb = args.output.stat().st_size / 1e6
    print(f"Output: {args.output} ({size_mb:.0f} MB)")
    print(f"  audio codec: {codec} {'OK (lossless)' if ok_codec else 'FAIL ; NOT LOSSLESS'}")
    print(f"  duration: {out_dur:.1f}s vs source {duration:.1f}s "
          f"{'OK' if ok_dur else 'FAIL ; length mismatch'}")
    if not (ok_codec and ok_dur):
        sys.exit(1)


if __name__ == "__main__":
    main()
