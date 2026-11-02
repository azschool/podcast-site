#!/usr/bin/env python3
"""
Production agent for "Running Your AI Agents: AEBOK Explained".

Reads a two-voice script (lines starting "Rabi:" or "Dave:"), generates one
dialogue audio file with ElevenLabs, applies the standing audio treatment,
uploads it to Transistor and publishes it.

LIMIT LINE (read before switching it on)
  Does alone:      generate audio, treat audio, upload file, create and publish
                   the episode named in the sidecar JSON.
  Checks with you: nothing in the normal path. If the script has no sidecar, if
                   a line has no speaker label, if the audio is under 8 minutes
                   or over 25, or if any API call fails, it STOPS and prints why.
                   It never guesses a title, a description or a speaker.
  Never touches:   show settings, other episodes, the voice library, the
                   newsletter, social posts, or any file outside ./out.

Usage:
  python produce_episode.py path/to/Ep01_Studio_Script.txt
Needs, next to this file, a .env with:
  ELEVENLABS_API_KEY=...
  TRANSISTOR_API_KEY=...
  TRANSISTOR_SHOW_ID=...
  VOICE_RABI=GifidYwohEPTNZsTeDLD
  VOICE_DAVE=A1fsqMcxAgM5FTRQc4fb
And a sidecar JSON next to the script, same name with .json:
  {"title": "...", "description": "...", "number": 1, "publish": true}
"""
import json, os, re, subprocess, sys, time
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
MAX_CHARS = 1800          # ElevenLabs asks for <= 2000 per dialogue request
MIN_MIN, MAX_MIN = 8, 25  # sanity window for a finished episode


def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: os.environ[k] for k in os.environ if k in (
        "ELEVENLABS_API_KEY", "TRANSISTOR_API_KEY", "TRANSISTOR_SHOW_ID", "VOICE_RABI", "VOICE_DAVE")})
    missing = [k for k in ("ELEVENLABS_API_KEY", "TRANSISTOR_API_KEY", "TRANSISTOR_SHOW_ID", "VOICE_RABI", "VOICE_DAVE") if k not in env]
    if missing:
        stop(f"Missing settings in .env: {', '.join(missing)}")
    return env


def stop(msg):
    print(f"\nSTOPPED: {msg}\nNothing was published.")
    sys.exit(1)


def parse_script(path, voices):
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        m = re.match(r"^(Rabi|Dave):\s*(.+)$", s)
        if not m:
            stop(f"Line without a speaker label: {s[:60]}")
        who, text = m.groups()
        lines.append({"text": text, "voice_id": voices[who]})
    if not lines:
        stop("Script is empty.")
    return lines


def chunk(lines):
    chunks, cur, n = [], [], 0
    for ln in lines:
        if cur and n + len(ln["text"]) > MAX_CHARS:
            chunks.append(cur); cur, n = [], 0
        cur.append(ln); n += len(ln["text"])
    if cur:
        chunks.append(cur)
    return chunks


def generate(chunks, key):
    OUT.mkdir(exist_ok=True)
    parts = []
    for i, c in enumerate(chunks, 1):
        r = requests.post(
            "https://api.elevenlabs.io/v1/text-to-dialogue?output_format=mp3_44100_128",
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"inputs": c, "model_id": "eleven_v3"}, timeout=600)
        if r.status_code != 200:
            stop(f"ElevenLabs refused chunk {i}: {r.status_code} {r.text[:200]}")
        p = OUT / f"part_{i:02d}.mp3"
        p.write_bytes(r.content); parts.append(p)
        print(f"  chunk {i}/{len(chunks)} done")
        time.sleep(1)
    return parts


def stitch_and_treat(parts, stem):
    lst = OUT / "parts.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    raw = OUT / f"{stem}_raw.mp3"
    final = OUT / f"{stem}.mp3"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(raw)])
    # Standing audio treatment: trim head/tail silence, warmth shelf +3.5 dB at 140 Hz,
    # loudness -16 LUFS, true peak -1.5. No internal pause editing.
    af = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3,"
          "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3,areverse,"
          "bass=g=3.5:f=140:w=0.5,loudnorm=I=-16:TP=-1.5:LRA=11")
    run(["ffmpeg", "-y", "-i", str(raw), "-af", af, "-codec:a", "libmp3lame", "-b:a", "128k", str(final)])
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "default=nw=1:nk=1", str(final)]).decode().strip())
    minutes = dur / 60
    if not (MIN_MIN <= minutes <= MAX_MIN):
        stop(f"Episode runs {minutes:.1f} minutes, outside {MIN_MIN} to {MAX_MIN}. Listen to {final} before anything is uploaded.")
    print(f"  audio ready: {final.name}, {minutes:.1f} minutes")
    return final


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        stop(f"{cmd[0]} failed: {r.stderr[-400:]}")


def upload_and_publish(final, meta, env):
    h = {"x-api-key": env["TRANSISTOR_API_KEY"]}
    base = "https://api.transistor.fm/v1"
    a = requests.get(f"{base}/episodes/authorize_upload", params={"filename": final.name}, headers=h, timeout=60)
    if a.status_code != 200:
        stop(f"Transistor authorize_upload failed: {a.status_code} {a.text[:200]}")
    attrs = a.json()["data"]["attributes"]
    with open(final, "rb") as f:
        u = requests.put(attrs["upload_url"], data=f, headers={"Content-Type": attrs.get("content_type", "audio/mpeg")}, timeout=600)
    if u.status_code not in (200, 201):
        stop(f"Audio upload failed: {u.status_code}")
    body = {"episode[show_id]": env["TRANSISTOR_SHOW_ID"], "episode[title]": meta["title"],
            "episode[summary]": meta["description"][:255], "episode[description]": meta["description"],
            "episode[audio_url]": attrs["audio_url"]}
    if meta.get("number"):
        body["episode[number]"] = meta["number"]
    c = requests.post(f"{base}/episodes", data=body, headers=h, timeout=60)
    if c.status_code not in (200, 201):
        stop(f"Create episode failed: {c.status_code} {c.text[:200]}")
    ep_id = c.json()["data"]["id"]
    print(f"  episode created as draft, id {ep_id}")
    if meta.get("publish", False):
        p = requests.patch(f"{base}/episodes/{ep_id}/publish", data={"episode[status]": "published"}, headers=h, timeout=60)
        if p.status_code != 200:
            stop(f"Publish failed: {p.status_code} {p.text[:200]}")
        print("  published")
    else:
        print("  left as draft (publish=false in sidecar)")
    return ep_id


def main():
    if len(sys.argv) != 2:
        stop("Usage: python produce_episode.py path/to/script.txt")
    script = Path(sys.argv[1])
    sidecar = script.with_suffix(".json")
    if not script.exists():
        stop(f"No script at {script}")
    if not sidecar.exists():
        stop(f"No sidecar at {sidecar}. It must hold title, description, number, publish.")
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    for k in ("title", "description"):
        if not meta.get(k):
            stop(f"Sidecar is missing {k}.")
    env = load_env()
    voices = {"Rabi": env["VOICE_RABI"], "Dave": env["VOICE_DAVE"]}
    print("1. Reading script")
    lines = parse_script(script, voices)
    chunks = chunk(lines)
    print(f"   {len(lines)} lines, {len(chunks)} chunks")
    print("2. Generating dialogue audio")
    parts = generate(chunks, env["ELEVENLABS_API_KEY"])
    print("3. Stitching and treating")
    final = stitch_and_treat(parts, script.stem)
    print("4. Uploading to Transistor")
    ep_id = upload_and_publish(final, meta, env)
    log = OUT / "log.txt"
    with open(log, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} {script.name} -> episode {ep_id} ({'published' if meta.get('publish') else 'draft'})\n")
    print(f"\nDone. Log: {log}")


if __name__ == "__main__":
    main()
