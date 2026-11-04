#!/usr/bin/env python3
"""
Production agent for "Running Your AI Agents: AEBOK Explained".

Reads a two-voice script (lines starting "Rabi:" or "Dave:"), generates one
dialogue audio file with ElevenLabs, applies the standing audio treatment,
and publishes it to the free GitHub Pages feed (or Transistor if configured).

LIMIT LINE (read before switching it on)
  Does alone:      generate audio, treat audio, write the feed, push the
                   episode named in the sidecar JSON.
  Checks with you: nothing in the normal path. If the script has no sidecar, if
                   a line has no speaker label, if the audio is under 8 minutes
                   or over 25, or if any API call fails, it STOPS and prints why.
                   It never guesses a title, a description or a speaker.
  Never touches:   show settings, other episodes, the voice library, the
                   newsletter, social posts, or any file outside ./out and
                   the site folder's episodes/ and feed.xml.

Usage:
  python produce_episode.py path/to/Ep01_Studio_Script.txt
  python produce_episode.py --retreat path/to/Ep01_Studio_Script.txt
    (re-run only the audio treatment on out/<stem>_raw.mp3 and copy the result into
     SITE_DIR/episodes/; no ElevenLabs call, no feed or index change)
Settings come from environment variables or a .env next to this file:
  ELEVENLABS_API_KEY, VOICE_RABI, VOICE_DAVE, SITE_DIR, SITE_URL
  (or TRANSISTOR_API_KEY and TRANSISTOR_SHOW_ID instead of SITE_DIR/SITE_URL)
Optional, with these defaults:
  ELEVEN_MODEL=eleven_v4  ELEVEN_STABILITY=0.55  ELEVEN_SIMILARITY=0.87  ELEVEN_CLONE_STRENGTH=0
  Speaker boost is not a field on the text-to-dialogue endpoint, so it is not sent.
Audio tags such as [thoughtful] and <break time="0.6s"/> in the script are passed through unchanged.
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
DEFAULTS = {                # overridable by environment variables of the same name
    "ELEVEN_MODEL": "eleven_v4",
    "ELEVEN_STABILITY": "0.55",
    "ELEVEN_SIMILARITY": "0.87",
    "ELEVEN_CLONE_STRENGTH": "0",
}


def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: os.environ[k] for k in os.environ if k in (
        "ELEVENLABS_API_KEY", "TRANSISTOR_API_KEY", "TRANSISTOR_SHOW_ID", "VOICE_RABI", "VOICE_DAVE", "SITE_DIR", "SITE_URL",
        *DEFAULTS)})
    for k, v in DEFAULTS.items():
        env.setdefault(k, v)
    missing = [k for k in ("ELEVENLABS_API_KEY", "VOICE_RABI", "VOICE_DAVE") if k not in env]
    if "TRANSISTOR_API_KEY" not in env and not ("SITE_DIR" in env and "SITE_URL" in env):
        missing.append("either TRANSISTOR_API_KEY+TRANSISTOR_SHOW_ID or SITE_DIR+SITE_URL")
    if missing:
        stop(f"Missing settings: {', '.join(missing)}")
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


def generate(chunks, env):
    OUT.mkdir(exist_ok=True)
    key = env["ELEVENLABS_API_KEY"]
    # Fields per the text-to-dialogue request schema (api.elevenlabs.io/openapi.json):
    # settings = {stability, similarity, clone_strength}. There is no use_speaker_boost on this endpoint.
    settings = {"stability": float(env["ELEVEN_STABILITY"]),
                "similarity": float(env["ELEVEN_SIMILARITY"]),
                "clone_strength": float(env["ELEVEN_CLONE_STRENGTH"])}
    print(f"   model {env['ELEVEN_MODEL']}, settings {settings}")
    parts = []
    for i, c in enumerate(chunks, 1):
        r = requests.post(
            "https://api.elevenlabs.io/v1/text-to-dialogue?output_format=mp3_44100_128",
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"inputs": c, "model_id": env["ELEVEN_MODEL"], "settings": settings}, timeout=600)
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
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(raw)])
    return treat(raw, stem)


# Standing audio treatment: trim head/tail silence, warmth shelf +3.5 dB at 140 Hz,
# then level the episode to -14 LUFS integrated with a true-peak ceiling of -1.0 dBTP.
# No internal pause editing.
PRE_LOUDNORM = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3,"
                "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3,areverse,"
                "bass=g=3.5:f=140:w=0.5")
TARGET_I, TARGET_TP, TARGET_LRA = -14.0, -1.0, 11
TOLERANCE_LU = 0.1        # how close the delivered MP3 must read to TARGET_I
MAX_PASSES = 5


def measure(path, prefilter=""):
    """Measure integrated loudness, true peak and LRA with loudnorm's analysis pass (EBU R128)."""
    af = (prefilter + "," if prefilter else "") + f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", af, "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        stop(f"ffmpeg loudness measurement failed: {r.stderr[-400:]}")
    try:
        js = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
        return float(js["input_i"]), float(js["input_tp"]), float(js["input_lra"])
    except (ValueError, KeyError):
        stop(f"Could not read loudness measurement: {r.stderr[-400:]}")


def treat(raw, stem):
    """Two-pass levelling. Pass 1 measures the treated signal. Pass 2 applies the measured gain
    as a constant (linear) gain into an oversampled true-peak limiter, then encodes the MP3.
    The delivered MP3 is measured again and the gain corrected if it is off target, because
    loudnorm's one-pass dynamic mode and the 128k MP3 encode each shave a few tenths of a LU.
    loudnorm's own linear mode cannot be used here: the raw ElevenLabs audio already peaks near
    0 dBTP, so the gain needed to reach -14 LUFS would breach -1.0 dBTP without a limiter."""
    final = OUT / f"{stem}.mp3"
    i0, tp0, lra0 = measure(raw, PRE_LOUDNORM)
    print(f"  measured: I {i0:.2f} LUFS, TP {tp0:.2f} dBTP, LRA {lra0:.1f}")
    gain, ceiling, history = TARGET_I - i0, TARGET_TP, []
    for n in range(1, MAX_PASSES + 1):
        af = (f"{PRE_LOUDNORM},volume={gain:.2f}dB,aresample=192000,"
              f"alimiter=limit={ceiling:.2f}dB:attack=5:release=100:level=false,aresample=44100")
        run(["ffmpeg", "-y", "-i", str(raw), "-af", af, "-codec:a", "libmp3lame", "-b:a", "128k", str(final)])
        i, tp, lra = measure(final)
        print(f"  pass {n}: gain {gain:+.2f} dB, ceiling {ceiling:.2f} dBTP -> MP3 reads I {i:.2f} LUFS, TP {tp:.2f} dBTP, LRA {lra:.1f}")
        if abs(i - TARGET_I) <= TOLERANCE_LU and tp <= TARGET_TP + 0.05:
            break
        # The limiter absorbs part of each extra dB, so each dB of gain yields less than 1 LU.
        # Use the slope seen between the last two passes to size the correction.
        history.append((gain, i))
        slope = 1.0
        if len(history) >= 2:
            (g1, i1), (g2, i2) = history[-2], history[-1]
            if abs(g2 - g1) > 1e-6:
                slope = min(1.0, max(0.3, (i2 - i1) / (g2 - g1)))
        gain += (TARGET_I - i) / slope
        if tp > TARGET_TP + 0.05:
            ceiling -= tp - TARGET_TP
    else:
        stop(f"Level did not settle within {TOLERANCE_LU} LU of {TARGET_I} LUFS in {MAX_PASSES} passes "
             f"(last reading {i:.2f} LUFS, {tp:.2f} dBTP). Listen to {final} before anything is uploaded.")
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


SHOW = {
    "title": "Running Your AI Agents: AEBOK™ Explained",
    "link": "https://autonomousenterpriseai.com",
    "author": "Rabi Jay",
    "email": "rabijay1@yahoo.com",
    "description": ("AI agents can now answer your customers, post for you, chase invoices and prepare your books. "
                    "The question is not whether they can do the work. It is what they are allowed to do, who approves it, "
                    "and who answers when it goes wrong. Rabi Jay, author of three books on enterprise AI and founder of AEBOK, "
                    "the Autonomous Enterprise Body of Knowledge, works through it with Dave, who runs a small online marketplace "
                    "and is switching on his agents one at a time. Dave is invented. The agents and the mistakes are real. "
                    "Part of the AEBOK™ series from the Autonomous Enterprise Institute."),
    "image": "cover.jpg",
    "category": "Business",
}
SERIES_LINE = "Part of the AEBOK™ series from the Autonomous Enterprise Institute."  # appended to every episode description in the feed


def publish_github_pages(final, meta, env):
    """Free hosting: copy the MP3 into the site folder, rebuild feed.xml, git push. GitHub Pages serves both."""
    import shutil
    from email.utils import formatdate
    site = Path(env["SITE_DIR"]).expanduser()
    base = env["SITE_URL"].rstrip("/")
    (site / "episodes").mkdir(parents=True, exist_ok=True)
    dest = site / "episodes" / final.name
    shutil.copy(final, dest)
    size = dest.stat().st_size
    dur = int(float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                             "-of", "default=nw=1:nk=1", str(dest)]).decode().strip()))
    idx = site / "episodes.json"
    items = json.loads(idx.read_text()) if idx.exists() else []
    items = [i for i in items if i["file"] != final.name]
    items.append({"file": final.name, "title": meta["title"], "description": meta["description"],
                  "number": meta.get("number"), "size": size, "duration": dur, "date": formatdate(localtime=True)})
    items.sort(key=lambda i: i.get("number") or 0)
    idx.write_text(json.dumps(items, indent=2))
    write_feed(site, base, items)
    if meta.get("publish", False):
        for cmd in (["git", "-C", str(site), "add", "-A"],
                    ["git", "-C", str(site), "commit", "-m", f"Episode: {meta['title']}"],
                    ["git", "-C", str(site), "push"]):
            run(cmd)
        print(f"  pushed. Feed: {base}/feed.xml")
    else:
        print(f"  feed and file written to {site}, not pushed (publish=false in sidecar)")
    return final.name


def write_feed(site, base, items):
    """Rebuild feed.xml in the site folder from the episodes list (episodes.json)."""
    import html
    e = html.escape
    rss = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">',
           '<channel>',
           f'<title>{e(SHOW["title"])}</title>', f'<link>{SHOW["link"]}</link>', '<language>en-us</language>',
           f'<description>{e(SHOW["description"])}</description>', f'<itunes:author>{e(SHOW["author"])}</itunes:author>',
           f'<itunes:summary>{e(SHOW["description"])}</itunes:summary>', '<itunes:explicit>false</itunes:explicit>',
           f'<itunes:category text="{SHOW["category"]}"/>', f'<itunes:image href="{base}/{SHOW["image"]}"/>',
           f'<atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>']
    if SHOW["email"]:
        rss.append(f'<itunes:owner><itunes:name>{e(SHOW["author"])}</itunes:name><itunes:email>{SHOW["email"]}</itunes:email></itunes:owner>')
    for i in reversed(items):
        url = f'{base}/episodes/{i["file"]}'
        desc = i["description"].rstrip() + "\n\n" + SERIES_LINE
        rss += ['<item>', f'<title>{e(i["title"])}</title>', f'<description>{e(desc)}</description>',
                f'<enclosure url="{url}" length="{i["size"]}" type="audio/mpeg"/>', f'<guid isPermaLink="false">{url}</guid>',
                f'<pubDate>{i["date"]}</pubDate>', f'<itunes:duration>{i["duration"]}</itunes:duration>',
                f'<itunes:episode>{i["number"]}</itunes:episode>' if i.get("number") else '', '<itunes:explicit>false</itunes:explicit>', '</item>']
    rss += ['</channel>', '</rss>']
    (site / "feed.xml").write_text("\n".join(x for x in rss if x), encoding="utf-8")


def retreat(script):
    """Re-run only the treatment on an existing raw stitch. Touches out/ and the episode MP3, nothing else."""
    import shutil
    raw = OUT / f"{script.stem}_raw.mp3"
    if not raw.exists():
        stop(f"No raw audio at {raw}. Run the full pipeline first.")
    env = load_env()
    print("Re-treating existing raw audio")
    final = treat(raw, script.stem)
    if "SITE_DIR" in env:
        dest = Path(env["SITE_DIR"]).expanduser() / "episodes" / final.name
        shutil.copy(final, dest)
        print(f"  copied to {dest}. feed.xml and episodes.json not changed.")
    with open(OUT / "log.txt", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} {script.name} -> re-treated {final.name}\n")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--retreat":
        return retreat(Path(sys.argv[2]))
    if len(sys.argv) != 2:
        stop("Usage: python produce_episode.py [--retreat] path/to/script.txt")
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
    parts = generate(chunks, env)
    print("3. Stitching and treating")
    final = stitch_and_treat(parts, script.stem)
    if "SITE_DIR" in env:
        print("4. Publishing to the free feed (GitHub Pages)")
        ep_id = publish_github_pages(final, meta, env)
    else:
        print("4. Uploading to Transistor")
        ep_id = upload_and_publish(final, meta, env)
    log = OUT / "log.txt"
    with open(log, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} {script.name} -> episode {ep_id} ({'published' if meta.get('publish') else 'draft'})\n")
    print(f"\nDone. Log: {log}")


if __name__ == "__main__":
    main()
