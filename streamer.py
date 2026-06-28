#!/usr/bin/env python3
"""YouTube 24/7 stream controller — daemon + CLI"""

import argparse
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    _PILLOW = True
except ImportError:
    _PILLOW = False

BASE_DIR      = Path(__file__).parent
CONFIG_FILE   = BASE_DIR / "config.json"
SCHEDULE_FILE = BASE_DIR / "schedule.json"
VIDEOS_DIR    = BASE_DIR / "videos"
BACKGROUND    = BASE_DIR / "background.mp4"
BACKGROUND_STILL = BASE_DIR / ".background_still.jpg"
BACKGROUND_AUDIO = BASE_DIR / ".background_audio.aac"
LOGO_FILE        = BASE_DIR / "muha-mini.png"
LOGO_H           = 70    # visina loga u px
LOGO_X           = 310   # desno od "TSETSE TV" teksta
LOGO_Y           = 40    # vertikalno centriran s tekstom
PID_FILE      = BASE_DIR / ".streamer.pid"
LOG_FILE      = BASE_DIR / "streamer.log"
OVERLAY_DIR         = BASE_DIR / ".overlay"
OVERLAY_SLOTS       = 7  # header + 6 schedule lines
SCHEDULE_PNG        = BASE_DIR / ".schedule_overlay.png"
PICTURE_OVERLAY     = BASE_DIR / "picture-overlay"
SELECTED_IMAGE_FILE = BASE_DIR / ".selected_image"
CONTROL_FILE        = BASE_DIR / ".control"
# PIP box: desna strana ekrana, ne prekriva overlay tekst lijevo
PIP_W, PIP_H = 1240, 698   # 16:9
PIP_X, PIP_Y = 640,  191   # x=640 ostavlja 580px za overlay, y centriran

DEFAULT_CONFIG = {
    "youtube_rtmp": "rtmp://a.rtmp.youtube.com/live2",
    "stream_key": "YOUR_STREAM_KEY_HERE",
    "bitrate": "1500k",
    "audio_bitrate": "128k",
    "framerate": 25,
}

OUTPUT_W, OUTPUT_H = 1280, 720


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
        ],
    )

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print("Stvoren config.json — upiši svoj YouTube stream ključ, pa pokreni ponovo.")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text())
    if cfg.get("stream_key") == "YOUR_STREAM_KEY_HERE":
        print("Greška: postavi stream_key u config.json prije pokretanja.")
        sys.exit(1)
    return cfg


# ── Schedule ──────────────────────────────────────────────────────────────────

def load_schedule():
    if not SCHEDULE_FILE.exists():
        return []
    return json.loads(SCHEDULE_FILE.read_text())

def save_schedule(schedule):
    SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "ponedjeljak": 0, "utorak": 1, "srijeda": 2, "cetvrtak": 3, "četvrtak": 3,
    "petak": 4, "subota": 5, "nedjelja": 6,
}
WEEKDAY_NAMES = ["pon", "uto", "sri", "čet", "pet", "sub", "ned"]


def parse_time_arg(time_str):
    """Return (date_str_or_None, weekday_or_None, 'HH:MM')"""
    time_str = time_str.strip()

    # "today HH:MM"
    if time_str.lower().startswith("today "):
        hm = time_str[6:].strip()
        try:
            datetime.datetime.strptime(hm, "%H:%M")
            return datetime.date.today().isoformat(), None, hm
        except ValueError:
            pass

    # "<weekday> HH:MM"
    parts = time_str.split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in WEEKDAYS:
        hm = parts[1].strip()
        try:
            datetime.datetime.strptime(hm, "%H:%M")
            return None, WEEKDAYS[parts[0].lower()], hm
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(time_str, fmt)
            return dt.strftime("%Y-%m-%d"), None, dt.strftime("%H:%M")
        except ValueError:
            pass
    try:
        datetime.datetime.strptime(time_str, "%H:%M")
        return None, None, time_str
    except ValueError:
        raise ValueError(
            f"Neispravan format vremena: '{time_str}'. Koristi HH:MM (svaki dan), today HH:MM, "
            f"<dan> HH:MM (npr. friday 15:00) ili YYYY-MM-DD HH:MM (jednom)."
        )

def item_key(item):
    if item.get("type") == "random":
        return ("random", tuple(item.get("folders", [])), item["time"],
                item.get("date"), item.get("weekday"))
    return (item["title"], item["time"], item.get("date"), item.get("weekday"))

def next_occurrence(item, now):
    h, m = map(int, item["time"].split(":"))
    if item.get("date"):
        return datetime.datetime.strptime(item["date"], "%Y-%m-%d").replace(
            hour=h, minute=m, second=0, microsecond=0
        )
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if item.get("weekday") is not None:
        days_ahead = (item["weekday"] - base.weekday()) % 7
        if days_ahead == 0 and base <= now:
            days_ahead = 7
        return base + datetime.timedelta(days=days_ahead)
    if base <= now:
        base += datetime.timedelta(days=1)
    return base

def get_upcoming(schedule, limit=5, now=None):
    if now is None:
        now = datetime.datetime.now()
    items = sorted(schedule, key=lambda it: next_occurrence(it, now))
    result = []
    for item in items[:limit]:
        dt = next_occurrence(item, now)
        days = (dt.date() - now.date()).days
        if days == 0:
            label = dt.strftime("%H:%M")
        elif days == 1:
            label = f"Sutra {dt.strftime('%H:%M')}"
        else:
            label = dt.strftime("%a %H:%M")
        result.append({"item": item, "dt": dt, "label": label})
    return result

def should_play_now(item, now=None):
    """True if scheduled time is within the last 2 minutes."""
    if now is None:
        now = datetime.datetime.now()
    h, m = map(int, item["time"].split(":"))
    if item.get("date"):
        scheduled = datetime.datetime.strptime(item["date"], "%Y-%m-%d").replace(
            hour=h, minute=m, second=0, microsecond=0
        )
    else:
        if item.get("weekday") is not None and now.weekday() != item["weekday"]:
            return False
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
    diff = (now - scheduled).total_seconds()
    return 0 <= diff < 120


# ── Video matching ────────────────────────────────────────────────────────────

def _norm(s):
    return s.lower().replace(" ", "").replace("_", "").replace("-", "").replace(".", "")

def find_video(title):
    """
    Egzaktan match po normaliziranom imenu (bez razmaka/podvlaka/točaka).
    Podržava i 'folder/ime' sintaksu za disambiguaciju kad isti naziv postoji
    u više subfoldera.
    """
    if not VIDEOS_DIR.exists():
        return None
    candidates = list(VIDEOS_DIR.glob("**/*.mp4")) + list(VIDEOS_DIR.glob("**/*.mkv"))
    if not candidates:
        return None

    # Folder/ime sintaksa
    if "/" in title:
        folder, name = title.split("/", 1)
        nf, nn = _norm(folder), _norm(name)
        for path in candidates:
            try:
                rel_folder = path.parent.relative_to(VIDEOS_DIR).as_posix()
            except ValueError:
                continue
            if _norm(rel_folder) == nf and _norm(path.stem) == nn:
                return path
        return None

    nt = _norm(title)
    for path in candidates:
        if _norm(path.stem) == nt:
            return path
    return None

def collect_videos_in_folders(folders):
    """Vrati listu video path-ova iz navedenih subfoldera unutar videos/."""
    paths = []
    for f in folders:
        d = VIDEOS_DIR / f.strip()
        if d.is_dir():
            paths += list(d.glob("**/*.mp4")) + list(d.glob("**/*.mkv"))
    return paths

def build_random_queue(folders, length_seconds):
    """Random sekvenca videa iz foldera sve dok ukupno trajanje ne dosegne length_seconds."""
    import random
    candidates = collect_videos_in_folders(folders)
    if not candidates:
        return []
    pool = candidates.copy()
    random.shuffle(pool)
    queue = []
    total = 0.0
    # Prva runda — bez ponavljanja
    for c in pool:
        if total >= length_seconds:
            break
        dur = get_video_duration(c) or 0
        if dur <= 0:
            continue
        queue.append(c)
        total += dur
    # Ako još premalo, ponavljaj nasumično
    while total < length_seconds:
        c = random.choice(candidates)
        dur = get_video_duration(c) or 0
        if dur <= 0:
            continue
        queue.append(c)
        total += dur
    return queue

def build_queue_for_item(item):
    """Vrati listu Path-ova za scheduled stavku (fixed ili random)."""
    if item.get("type") == "random":
        length_sec = int(item.get("length_minutes", 60)) * 60
        return build_random_queue(item.get("folders", []), length_sec)
    titles = expand_title_to_queue(item["title"])
    paths = []
    for t in titles:
        v = find_video(t)
        if v:
            paths.append(v)
    return paths

def expand_title_to_queue(title):
    """
    Razdvoji title u listu naslova:
    - 'a, b, c' → ['a', 'b', 'c']
    - 'a x3' ili 'a×3' → ['a', 'a', 'a']
    Kombinacije: 'a x2, b' → ['a', 'a', 'b']
    """
    import re
    parts = [p.strip() for p in title.split(",") if p.strip()]
    queue = []
    for p in parts:
        m = re.match(r"^(.*?)\s*[x×]\s*(\d+)\s*$", p, re.IGNORECASE)
        if m:
            base = m.group(1).strip()
            count = int(m.group(2))
            queue.extend([base] * count)
        else:
            queue.append(p)
    return queue


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def render_schedule_png(schedule):
    """Generiraj PNG s rasporedom koristeći Pillow — jeftinije od 7x drawtext u FFmPegu."""
    if not _PILLOW:
        return
    now = datetime.datetime.now()
    cutoff = now + datetime.timedelta(hours=5)
    upcoming = [
        u for u in get_upcoming(schedule, limit=6, now=now)
        if u["dt"] <= cutoff
    ]

    def fmt(u):
        title = u['item'].get('display_title') or u['item']['title']
        if len(title) > 90:
            title = title[:88] + ".."
        return f"{u['label']}  {title}"

    lines = [fmt(u) for u in upcoming]

    # PNG u veličini cijelog platna — text lijevo, ostatak transparentan
    IMG_W, IMG_H = 1920, 1080
    img = Image.new("RGBA", (IMG_W, IMG_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Pokušaj učitati font, fallback na default
    try:
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        font_body   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 38)
    except Exception:
        font_header = ImageFont.load_default()
        font_body   = font_header

    def draw_text_shadow(d, pos, text, font, color=(255, 255, 255, 220)):
        x, y = pos
        # sjena
        d.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 180))
        # tekst
        d.text((x, y), text, font=font, fill=color)

    # Header: "TSETSE TV" (logo ide zasebno u FFmpeg filteru)
    draw_text_shadow(draw, (60, 60), "TSETSE TV", font_header)

    # Schedule linije
    for i, line in enumerate(lines):
        y = 60 + (i + 1) * 58
        draw_text_shadow(draw, (60, y), line, font_body)

    # Atomski zapis — zamijeni stari fajl odjednom
    tmp = SCHEDULE_PNG.with_suffix(".tmp.png")
    img.save(str(tmp), "PNG")
    tmp.replace(SCHEDULE_PNG)

def write_overlay_files(schedule):
    """Generiraj schedule PNG (Pillow) i fallback text fajlove."""
    OVERLAY_DIR.mkdir(exist_ok=True)
    now = datetime.datetime.now()
    cutoff = now + datetime.timedelta(hours=5)
    upcoming = [
        u for u in get_upcoming(schedule, limit=6, now=now)
        if u["dt"] <= cutoff
    ]

    def fmt(u):
        title = u['item'].get('display_title') or u['item']['title']
        if len(title) > 90:
            title = title[:88] + ".."
        return f"{u['label']}  {title}"

    lines = [fmt(u) for u in upcoming]
    slots = ["TSETSE TV"] + lines + [""] * (OVERLAY_SLOTS - 1 - len(lines))
    for i, text in enumerate(slots[:OVERLAY_SLOTS]):
        (OVERLAY_DIR / f"line{i}.txt").write_text(text)

    render_schedule_png(schedule)

def get_picture_overlay():
    """Return the selected image from picture-overlay/, or None."""
    if not PICTURE_OVERLAY.exists():
        return None
    if SELECTED_IMAGE_FILE.exists():
        name = SELECTED_IMAGE_FILE.read_text().strip()
        if name == "off":
            return None
        path = PICTURE_OVERLAY / name
        if path.exists():
            return path
    return None

def list_overlay_images():
    if not PICTURE_OVERLAY.exists():
        return []
    return sorted([
        p for ext in ("*.png", "*.jpg", "*.jpeg")
        for p in PICTURE_OVERLAY.glob(ext)
    ])

def _drawtext_file(slot, x, y, size=30, color="white@0.85"):
    path = str(OVERLAY_DIR / f"line{slot}.txt").replace("\\", "/").replace(":", "\\:")
    return (
        f"drawtext=textfile='{path}':reload=1:"
        f"fontcolor={color}:fontsize={size}:"
        f"x={x}:y={y}:"
        f"shadowcolor=black@0.9:shadowx=2:shadowy=2"
    )

def ensure_background_still():
    """Izvuci statični frame iz background.mp4 jednom (za video mod, da nema dekodiranja)."""
    if BACKGROUND_STILL.exists():
        return
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-ss", "2", "-i", str(BACKGROUND),
        "-vframes", "1", "-q:v", "3",
        str(BACKGROUND_STILL),
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def ensure_background_audio():
    """Izvuci audio iz background.mp4 jednom — koristi se u idle modu umjesto cijelog videa."""
    if BACKGROUND_AUDIO.exists():
        return
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(BACKGROUND),
        "-vn", "-c:a", "copy",
        str(BACKGROUND_AUDIO),
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        # Fallback: re-encode if copy fails (e.g. unsupported codec)
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-i", str(BACKGROUND),
             "-vn", "-c:a", "aac", "-b:a", "128k", str(BACKGROUND_AUDIO)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

def get_bg_duration():
    """Probe background.mp4 duration once so we can seek to the right loop position."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(BACKGROUND)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        return float(result.stdout.decode().strip())
    except Exception:
        return 30.0

def get_video_duration(video_path):
    """Vrati trajanje videa u sekundama, ili None."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        return float(result.stdout.decode().strip())
    except Exception:
        return None

def get_video_dimensions(video_path):
    """Vrati (width, height) videa preko ffprobea. Fallback na 16:9."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(video_path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        w, h = result.stdout.decode().strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080

def compute_pip_box(video_path):
    """
    Vrati (pip_w, pip_h, pip_x, pip_y) na 1920x1080 platnu.
    - Vertikalni video: visina 1080, širina ovisi o aspektu
    - Horizontalni: širina do 1240 (current), visina ovisi o aspektu
    - Max širina ograničena tako da ostane ≥300px lijevo za schedule overlay
    - Pozicioniran desno (pip_x = 1920 - pip_w), vertikalno centriran
    """
    CANVAS_W, CANVAS_H = 1920, 1080
    MIN_LEFT_PAD = 300  # prostor za schedule overlay
    MAX_PIP_W = CANVAS_W - MIN_LEFT_PAD  # 1620

    vw, vh = get_video_dimensions(video_path)
    aspect = vw / vh if vh > 0 else 16/9

    if aspect < 1.0:
        # Vertikalni: visina puna, širina iz aspekta
        pip_h = CANVAS_H
        pip_w = round(pip_h * aspect)
    else:
        # Horizontalni: zadana širina 1240, visina iz aspekta
        pip_w = 1240
        pip_h = round(pip_w / aspect)
        if pip_h > CANVAS_H:
            pip_h = CANVAS_H
            pip_w = round(pip_h * aspect)

    # Clamp širine da ostane min padding lijevo
    if pip_w > MAX_PIP_W:
        pip_w = MAX_PIP_W
        pip_h = min(CANVAS_H, round(pip_w / aspect))

    # Parni brojevi (yuv420p zahtijeva)
    pip_w -= pip_w % 2
    pip_h -= pip_h % 2

    pip_x = CANVAS_W - pip_w
    pip_y = (CANVAS_H - pip_h) // 2
    pip_y -= pip_y % 2
    return pip_w, pip_h, pip_x, pip_y

def _schedule_overlay_inputs_and_chain(base_label, start_idx):
    """
    Vrati (extra_inputs, chain_fragment, last_label, next_idx) za schedule PNG overlay.
    Ako PNG ne postoji ili Pillow nije dostupan, vrati drawtext fallback.
    """
    if _PILLOW and SCHEDULE_PNG.exists():
        inputs = ["-loop", "1", "-i", str(SCHEDULE_PNG)]
        chain = f";[{base_label}][{start_idx}:v]overlay=0:0[{base_label}s]"
        return inputs, chain, f"[{base_label}s]", start_idx + 1
    else:
        # Fallback: stari drawtext
        text_filters = [_drawtext_file(0, 60, 60, size=42, color="white")]
        for i in range(1, OVERLAY_SLOTS):
            text_filters.append(_drawtext_file(i, 60, 60 + i * 58, size=38))
        chain = f"[{base_label}]{','.join(text_filters)}[{base_label}t]"
        return [], ";" + chain, f"[{base_label}t]", start_idx

def build_idle_cmd(config, picture=None, seek=0.0):
    """Idle ekran: bg video + PNG overlay rasporeda + logo + opcionalna slika → YouTube."""
    rtmp = f"{config['youtube_rtmp']}/{config['stream_key']}"

    # Input [0] = background video (loop), [1] = audio loop, [2..] = overlay/logo/picture
    inputs = ["-re", "-stream_loop", "-1", "-i", str(BACKGROUND)]

    # Audio
    if BACKGROUND_AUDIO.exists():
        inputs += ["-stream_loop", "-1", "-i", str(BACKGROUND_AUDIO)]
        audio_map = "1:a"
    else:
        audio_map = "0:a"

    idx = 2
    chain = "[0:v][v0_raw];" \
            "[v0_raw]null[v0]"  # placeholder, zamijenimo ispod
    # Reset: jednostavan start
    chain = "[0:v]scale=1920:1080[v0]"
    last = "[v0]"

    # Schedule PNG overlay
    sched_inputs, sched_chain, last, idx = _schedule_overlay_inputs_and_chain("v0", idx)
    inputs += sched_inputs
    chain += sched_chain

    # Logo
    has_logo = LOGO_FILE.exists()
    if has_logo:
        inputs += ["-loop", "1", "-i", str(LOGO_FILE)]
        chain += f";[{idx}:v]scale=-1:{LOGO_H}[logo];{last}[logo]overlay={LOGO_X}:{LOGO_Y}[vl]"
        last = "[vl]"
        idx += 1

    # Picture overlay
    if picture:
        inputs += ["-loop", "1", "-i", str(picture)]
        chain += f";{last}[{idx}:v]overlay=(W-w)/2:(H-h)/2[vp]"
        last = "[vp]"

    chain += f";{last}scale={OUTPUT_W}:{OUTPUT_H}[out]"
    last = "[out]"

    return [
        "ffmpeg", "-hide_banner", *inputs,
        "-filter_complex", chain, "-map", last, "-map", audio_map,
        "-af", "aresample=async=1000:first_pts=0",
        "-c:v", "libx264", "-preset", "superfast", "-r", str(config.get("framerate", 25)), "-g", str(int(config.get("framerate", 25)) * 2),
        "-threads", "4", "-x264-params", "threads=4:sliced-threads=1",
        "-b:v", config["bitrate"], "-maxrate", config["bitrate"], "-bufsize", "2400k",
        "-c:a", "aac", "-b:a", config["audio_bitrate"], "-ar", "48000",
        "-f", "flv", "-rtmp_live", "live", rtmp,
    ]

def build_video_cmd(config, video_path, seek=0.0, video_start=0):
    """Video u PIP boxu na statičnoj pozadini + PNG overlay rasporeda + logo → YouTube."""
    rtmp = f"{config['youtube_rtmp']}/{config['stream_key']}"

    pip_w, pip_h, pip_x, pip_y = compute_pip_box(video_path)
    pip_vf = f"scale={pip_w}:{pip_h}:force_original_aspect_ratio=decrease,pad={pip_w}:{pip_h}:(ow-iw)/2:(oh-ih)/2:black"

    # Statična slika kao pozadina — nema dekodiranja videa u realtime
    bg_input = str(BACKGROUND_STILL) if BACKGROUND_STILL.exists() else str(BACKGROUND)
    if BACKGROUND_STILL.exists():
        bg_args = ["-loop", "1", "-framerate", "25", "-i", bg_input]
    else:
        bg_args = ["-re", "-stream_loop", "-1", "-i", bg_input]

    # Inputs: [0]=bg, [1]=video, [2+]=schedule PNG / logo
    extra_inputs = []
    idx = 2

    chain = (
        f"[0:v]scale=1920:1080[bg];"
        f"[1:v]{pip_vf}[pip];"
        f"[bg][pip]overlay={pip_x}:{pip_y}[v0]"
    )
    last = "[v0]"

    # Schedule PNG overlay
    sched_inputs, sched_chain, last, idx = _schedule_overlay_inputs_and_chain("v0", idx)
    extra_inputs += sched_inputs
    chain += sched_chain

    # Logo
    has_logo = LOGO_FILE.exists()
    if has_logo:
        extra_inputs += ["-loop", "1", "-i", str(LOGO_FILE)]
        chain += f";[{idx}:v]scale=-1:{LOGO_H}[logo];{last}[logo]overlay={LOGO_X}:{LOGO_Y}[v1]"
        last = "[v1]"
        idx += 1

    chain += f";{last}scale={OUTPUT_W}:{OUTPUT_H}[out]"
    last = "[out]"

    # Fade-out zvuka u zadnjim 2 sekundama videa
    dur = get_video_duration(video_path)
    effective_dur = (dur - video_start) if dur else None
    audio_args = ["-map", "1:a"]
    if effective_dur and effective_dur > 3:
        # afade pozicija je relativna na output (od trenutka kad start kreće)
        audio_args += ["-af", f"afade=t=out:st={effective_dur - 2:.2f}:d=2"]

    return [
        "ffmpeg", "-hide_banner",
        *bg_args,
        *(["-ss", str(video_start)] if video_start > 0 else []),
        "-re", "-i", str(video_path),
        *extra_inputs,
        "-filter_complex", chain,
        "-map", last, *audio_args,
        "-shortest",
        "-c:v", "libx264", "-preset", "superfast", "-r", str(config.get("framerate", 25)), "-g", str(int(config.get("framerate", 25)) * 2),
        "-threads", "4", "-x264-params", "threads=4:sliced-threads=1",
        "-b:v", config["bitrate"], "-maxrate", config["bitrate"], "-bufsize", "2400k",
        "-c:a", "aac", "-b:a", config["audio_bitrate"], "-ar", "48000",
        "-f", "flv", "-rtmp_live", "live", rtmp,
    ]

def _open_log():
    return open(LOG_FILE, "a")

def start_ffmpeg(cmd):
    """Launch FFmpeg with all output captured to log — never exposed to stream or terminal."""
    fd = _open_log()
    return subprocess.Popen(
        cmd,
        stdout=fd,
        stderr=fd,
        stdin=subprocess.DEVNULL,
    )

def kill_ffmpeg(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Background generation ─────────────────────────────────────────────────────

def generate_background():
    if BACKGROUND.exists():
        return
    print("Generiranje background.mp4 (prvo pokretanje — zamijeni ovaj video svojim)…")
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-f", "lavfi",
        "-i", (
            "color=c=0x0d1117:size=1920x1080:rate=30,"
            "geq="
            "r='30+20*sin(2*PI*X/960+T*0.25)*sin(2*PI*Y/540+T*0.18)':"
            "g='10+8*sin(2*PI*X/1920+T*0.12)':"
            "b='70+55*sin(2*PI*(X+Y)/1080+T*0.22)+25*sin(T*0.4)'"
        ),
        "-f", "lavfi", "-i", "sine=frequency=0:sample_rate=44100",
        "-t", "30",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        str(BACKGROUND),
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        # Solid color fallback
        cmd2 = [
            "ffmpeg", "-y", "-hide_banner",
            "-f", "lavfi", "-i", "color=c=0x1a1a2e:size=1920x1080:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=0:sample_rate=44100",
            "-t", "30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            str(BACKGROUND),
        ]
        subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("background.mp4 spreman.")


# ── Daemon ────────────────────────────────────────────────────────────────────

class Streamer:
    def __init__(self, config):
        self.config          = config
        self.proc            = None
        self.mode            = None   # "idle" | "video"
        self.playing_key     = None
        self.current_picture = None
        self.played_today    = {}
        self.idle_started_at = 0.0
        self.bg_duration     = 30.0
        self.running         = True
        self.queue           = []      # preostali videi iz multi-naslov scheduled itema
        self.queue_item      = None    # scheduled item iz kojeg je queue nastao
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def _shutdown(self, *_):
        log.info("Shutting down")
        self.running = False
        kill_ffmpeg(self.proc)
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    def _bg_seek(self):
        if self.idle_started_at > 0 and self.bg_duration > 0:
            return (time.time() - self.idle_started_at) % self.bg_duration
        return 0.0

    def _idle(self):
        seek = self._bg_seek()
        kill_ffmpeg(self.proc)
        schedule = load_schedule()
        write_overlay_files(schedule)
        picture = get_picture_overlay()
        cmd = build_idle_cmd(self.config, picture, seek)
        log.info(f"Idle stream  seek={seek:.1f}s  slika={picture.name if picture else 'nema'}")
        self.proc = start_ffmpeg(cmd)
        self.mode = "idle"
        self.playing_key = None
        self.current_picture = picture
        # Postavi idle_started_at tako da seek koji smo dali odgovara sadašnjem trenutku
        self.idle_started_at = time.time() - seek

    def _play(self, item, video_path, video_start=0):
        seek = self._bg_seek()
        kill_ffmpeg(self.proc)
        cmd = build_video_cmd(self.config, video_path, seek, video_start=video_start)
        log.info(f"Video '{item['title']}' ({video_path.name})  bg_seek={seek:.1f}s  start={video_start}s")
        self.proc = start_ffmpeg(cmd)
        self.mode = "video"
        self.playing_key = item_key(item)
        self.idle_started_at = 0.0
        today = datetime.date.today().isoformat()
        self.played_today[self.playing_key] = today

    def run(self):
        PID_FILE.write_text(str(os.getpid()))
        log.info(f"Streamer daemon pokrenut  PID={os.getpid()}")
        PICTURE_OVERLAY.mkdir(exist_ok=True)
        VIDEOS_DIR.mkdir(exist_ok=True)
        self.bg_duration = get_bg_duration()
        ensure_background_still()
        ensure_background_audio()
        self._idle()

        while self.running:
            time.sleep(5)
            now      = datetime.datetime.now()
            schedule = load_schedule()
            today    = datetime.date.today().isoformat()

            write_overlay_files(schedule)
            self.played_today = {k: v for k, v in self.played_today.items() if v == today}

            # Promjena slike u idle modu
            if self.mode == "idle":
                picture = get_picture_overlay()
                if picture != self.current_picture:
                    log.info("Slika promijenjena, restart idle")
                    self._idle()
                    continue

            # FFmpeg pao neočekivano
            if self.proc and self.proc.poll() is not None:
                if self.mode == "video":
                    log.info(f"Video završio: {self.playing_key}")
                    # Queue: pokreni sljedeći video iz iste schedule stavke
                    if self.queue:
                        next_video = self.queue.pop(0)
                        log.info(f"Queue: sljedeći '{next_video.name}' ({len(self.queue)} preostalo)")
                        self._play(self.queue_item, next_video)
                        continue
                    # Queue prazan ili pao — ukloni jednokratan stavku i nazad na idle
                    schedule = [
                        s for s in schedule
                        if not (item_key(s) == self.playing_key and s.get("date"))
                    ]
                    save_schedule(schedule)
                    self.queue = []
                    self.queue_item = None
                else:
                    log.warning("Idle stream pao, restartanje")
                self._idle()
                continue

            # Provjera CLI kontrola (skip / next)
            if CONTROL_FILE.exists():
                cmd = CONTROL_FILE.read_text().strip()
                CONTROL_FILE.unlink(missing_ok=True)
                if cmd == "refresh":
                    log.info("CLI refresh: regeneriram overlay i restartam stream")
                    if self.mode == "idle":
                        self._idle()
                    continue
                if cmd == "skip" and self.mode == "video":
                    log.info(f"CLI skip: prekidam video {self.playing_key}")
                    # Ukloni samo ako je jednokratan
                    if self.playing_key:
                        schedule = [
                            s for s in schedule
                            if not (item_key(s) == self.playing_key and s.get("date"))
                        ]
                        save_schedule(schedule)
                    self.queue = []
                    self.queue_item = None
                    self._idle()
                    continue
                if cmd == "next":
                    upcoming = get_upcoming(schedule, limit=1, now=now)
                    if upcoming:
                        nxt = upcoming[0]["item"]
                        queue_paths = build_queue_for_item(nxt)
                        if queue_paths:
                            log.info(f"CLI next: pokrećem '{nxt.get('display_title') or nxt.get('title')}'")
                            self.queue = queue_paths[1:]
                            self.queue_item = nxt
                            self._play(nxt, queue_paths[0], video_start=nxt.get("start_offset", 0))
                            continue
                        log.warning(f"CLI next: nema videa za stavku")
                    else:
                        log.info("CLI next: nema sljedećih u rasporedu")

            # Provjera rasporeda
            for item in schedule:
                if should_play_now(item, now):
                    key = item_key(item)
                    if self.mode == "video" and self.playing_key == key:
                        break
                    if self.played_today.get(key) == today:
                        break
                    queue_paths = build_queue_for_item(item)
                    if queue_paths:
                        first = queue_paths[0]
                        self.queue = queue_paths[1:]
                        self.queue_item = item
                        self._play(item, first, video_start=item.get("start_offset", 0))
                    else:
                        log.warning(f"Nema videa za stavku: {item.get('display_title') or item.get('title')}")
                    break


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_start(_args):
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"Streamer već radi  (PID {pid})")
            return
        except ProcessLookupError:
            PID_FILE.unlink(missing_ok=True)

    config = load_config()
    VIDEOS_DIR.mkdir(exist_ok=True)
    generate_background()

    pid = os.fork()
    if pid > 0:
        print(f"Streamer pokrenut u pozadini  (PID {pid})  — logovi: {LOG_FILE}")
        return

    # Child process
    os.setsid()
    setup_logging()
    Streamer(config).run()


def cmd_stop(_args):
    if not PID_FILE.exists():
        print("Streamer nije pokrenut.")
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Streamer zaustavljen  (PID {pid})")
    except ProcessLookupError:
        print("Streamer nije bio pokrenut (zaostali PID uklonjen).")
    PID_FILE.unlink(missing_ok=True)


def parse_offset(s):
    """Parsiraj 'MM:SS' ili 'HH:MM:SS' u sekunde. Vrati 0 ako prazno."""
    if not s:
        return 0
    parts = s.strip().split(":")
    try:
        if len(parts) == 2:
            m, sec = int(parts[0]), int(parts[1])
            return m * 60 + sec
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 3600 + m * 60 + sec
    except ValueError:
        pass
    raise ValueError(f"Neispravan format offseta: '{s}'. Koristi MM:SS ili HH:MM:SS.")


def _when_label(date_str, weekday, time_str):
    if date_str:
        return f"jednom {date_str} u {time_str}"
    if weekday is not None:
        return f"svaki {WEEKDAY_NAMES[weekday]} u {time_str}"
    return f"svaki dan u {time_str}"


def cmd_add(args):
    date_str, weekday, time_str = parse_time_arg(args.time)
    display_title = args.title if not args.title_override else args.title_override
    start_offset = parse_offset(args.start) if args.start else 0
    schedule = load_schedule()
    entry = {"title": args.title, "display_title": display_title, "time": time_str, "date": date_str}
    if weekday is not None:
        entry["weekday"] = weekday
    if start_offset > 0:
        entry["start_offset"] = start_offset
    schedule.append(entry)
    save_schedule(schedule)
    offset_str = f"  (od {args.start})" if start_offset else ""
    print(f"Dodano: '{display_title}'  {_when_label(date_str, weekday, time_str)}{offset_str}")


def cmd_add_random(args):
    date_str, weekday, time_str = parse_time_arg(args.time)
    folders = [f.strip() for f in args.folders.split(",") if f.strip()]
    if not folders:
        print("Greška: navedi barem jedan folder.")
        return
    missing = [f for f in folders if not (VIDEOS_DIR / f).is_dir()]
    if missing:
        print(f"Upozorenje: folderi ne postoje u videos/: {', '.join(missing)}")
    display_title = args.title_override or f"Random: {', '.join(folders)} ({args.length} min)"
    schedule = load_schedule()
    entry = {
        "type": "random",
        "folders": folders,
        "length_minutes": int(args.length),
        "display_title": display_title,
        "time": time_str,
        "date": date_str,
    }
    if weekday is not None:
        entry["weekday"] = weekday
    schedule.append(entry)
    save_schedule(schedule)
    print(f"Dodano (random): '{display_title}'  {_when_label(date_str, weekday, time_str)}")


def cmd_list(_args):
    schedule = load_schedule()
    if not schedule:
        print("Raspored je prazan.")
        return
    now = datetime.datetime.now()
    upcoming = get_upcoming(schedule, limit=100, now=now)
    upcoming_map = {item_key(u["item"]): u for u in upcoming}
    print(f"\n {'ID':>3}   {'Sljedeće':^18}   {'Trajanje':^9}   {'Kraj':^11}   {'Ponavljanje':^11}   Naziv")
    print("  " + "─" * 90)
    for i, item in enumerate(schedule):
        u = upcoming_map.get(item_key(item))
        when = u["label"] if u else item.get("date", "?")
        if item.get("date"):
            repeat = "jednom"
        elif item.get("weekday") is not None:
            repeat = WEEKDAY_NAMES[item["weekday"]]
        else:
            repeat = "svaki dan"
        title = (item.get('display_title') or item.get('title') or "")[:40]
        duration_str = "?"
        end_str = "?"
        if item.get("type") == "random":
            mins = int(item.get("length_minutes", 0))
            duration_str = f"~{mins}m"
            total_dur = mins * 60
        else:
            titles = expand_title_to_queue(item.get("title", ""))
            total_dur = 0.0
            missing = False
            for t in titles:
                v = find_video(t)
                if not v:
                    missing = True
                    continue
                d = get_video_duration(v)
                if d:
                    total_dur += d
            if total_dur > 0:
                mins = int(total_dur // 60)
                secs = int(total_dur % 60)
                duration_str = f"{mins}m {secs:02d}s"
                if missing:
                    duration_str += "*"
            elif missing:
                duration_str = "nema"
        if total_dur > 0 and u:
            end_dt = u["dt"] + datetime.timedelta(seconds=total_dur)
            days = (end_dt.date() - now.date()).days
            if days == 0:
                end_str = end_dt.strftime("%H:%M")
            elif days == 1:
                end_str = f"Sutra {end_dt.strftime('%H:%M')}"
            else:
                end_str = end_dt.strftime("%a %H:%M")
        print(f"  {i:>3}   {when:<18}   {duration_str:<9}   {end_str:<11}   {repeat:<11}   {title}")
    print()


def cmd_remove(args):
    schedule = load_schedule()
    if args.id < 0 or args.id >= len(schedule):
        print(f"Ne postoji unos s ID-em {args.id}  (pokreni 'stream list' za prikaz ID-eva)")
        return
    removed = schedule.pop(args.id)
    save_schedule(schedule)
    name = removed.get("display_title") or removed.get("title") or "(random)"
    print(f"Uklonjeno: '{name}'")


def cmd_image(args):
    PICTURE_OVERLAY.mkdir(exist_ok=True)
    images = list_overlay_images()

    if not hasattr(args, "name") or args.name is None:
        # Show current selection and available images
        current = SELECTED_IMAGE_FILE.read_text().strip() if SELECTED_IMAGE_FILE.exists() else "nema"
        print(f"Aktivna slika: {current}")
        if images:
            print("\nDostupne slike:")
            for img in images:
                marker = "→" if img.name == current else " "
                print(f"  {marker} {img.name}")
        else:
            print("Nema slika u picture-overlay/")
        return

    name = args.name.strip()

    if name == "off":
        SELECTED_IMAGE_FILE.write_text("off")
        print("Slika overlay-a isključena.")
        return

    # Fuzzy match against available images
    match = None
    for img in images:
        if img.name == name or img.stem == name:
            match = img
            break
    if not match:
        norm_name = _norm(name)
        for img in images:
            if norm_name in _norm(img.stem) or _norm(img.stem) in norm_name:
                match = img
                break
    if not match and images:
        best, best_r = None, 0.0
        for img in images:
            r = SequenceMatcher(None, _norm(name), _norm(img.stem)).ratio()
            if r > best_r:
                best_r, best = r, img
        if best_r > 0.4:
            match = best

    if not match:
        print(f"Slika '{name}' nije pronađena u picture-overlay/")
        if images:
            print("Dostupne slike: " + ", ".join(i.name for i in images))
        return

    SELECTED_IMAGE_FILE.write_text(match.name)
    print(f"Aktivna slika postavljena na: {match.name}")
    print("Promjena će se primijeniti na defaultnom ekranu unutar 5 sekundi.")


def cmd_skip(_args):
    if not PID_FILE.exists():
        print("Streamer nije pokrenut.")
        return
    CONTROL_FILE.write_text("skip")
    print("Prekid trenutnog videa zatražen (u sljedećih 5s).")


def cmd_next(_args):
    if not PID_FILE.exists():
        print("Streamer nije pokrenut.")
        return
    schedule = load_schedule()
    upcoming = get_upcoming(schedule, limit=1)
    if not upcoming:
        print("Nema sljedećih videa u rasporedu.")
        return
    CONTROL_FILE.write_text("next")
    nxt = upcoming[0]["item"]
    title = nxt.get("display_title") or nxt["title"]
    print(f"Pokrećem '{title}' (u sljedećih 5s).")


def cmd_refresh(_args):
    """Prisilno regeneriraj schedule PNG overlay i restartaj FFmpeg da pokupi novu sliku."""
    schedule = load_schedule()
    write_overlay_files(schedule)
    print("Schedule overlay regeneriran.")
    if PID_FILE.exists():
        CONTROL_FILE.write_text("refresh")
        print("Restart streama zatražen (u sljedećih 5s).")


def cmd_status(_args):
    if not PID_FILE.exists():
        print("Streamer: zaustavljen")
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, 0)
        print(f"Streamer: radi  (PID {pid})")
    except ProcessLookupError:
        print("Streamer: zaustavljen  (zaostali PID uklonjen)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="stream", description="YouTube 24/7 stream kontroler")
    sub = p.add_subparsers(dest="cmd", metavar="NAREDBA")

    sub.add_parser("start",  help="Pokreni streaming daemon")
    sub.add_parser("stop",   help="Zaustavi streaming daemon")
    sub.add_parser("status", help="Prikaži status daemona")
    sub.add_parser("list",   help="Prikaži raspored streamova")
    sub.add_parser("skip",   help="Prekini trenutni video, vrati na idle")
    sub.add_parser("next",   help="Odmah pokreni sljedeći video iz rasporeda")
    sub.add_parser("refresh",help="Regeneriraj schedule overlay i restartaj stream")

    pa = sub.add_parser("add", help="Dodaj zakazani stream")
    pa.add_argument("title", help="Naziv datoteke za pretragu u videos/")
    pa.add_argument("time",  help="HH:MM, today HH:MM ili YYYY-MM-DD HH:MM")
    pa.add_argument("--title", dest="title_override", default=None, metavar="NASLOV",
                    help="Custom naslov koji se prikazuje na streamu")
    pa.add_argument("--start", default=None, metavar="MM:SS",
                    help="Offset od početka videa (npr. 05:00 za petu minutu)")

    par = sub.add_parser("add-random", help="Dodaj random sekvencu iz foldera (npr. 80 min)")
    par.add_argument("folders", help="Folder ili više foldera odvojenih zarezom (npr. 'folder1, folder2')")
    par.add_argument("time", help="HH:MM, today HH:MM, <dan> HH:MM ili YYYY-MM-DD HH:MM")
    par.add_argument("--length", required=True, type=int, metavar="MIN",
                    help="Trajanje u minutama (npr. 80)")
    par.add_argument("--title", dest="title_override", default=None, metavar="NASLOV",
                    help="Custom naslov za prikaz")

    pr = sub.add_parser("remove", help="Ukloni zakazani stream prema ID-u")
    pr.add_argument("id", type=int, help="ID iz 'stream list'")

    pi = sub.add_parser("image", help="Odaberi sliku overlay-a (ili 'off' za isključivanje)")
    pi.add_argument("name", nargs="?", default=None, help="Naziv slike iz picture-overlay/ (ili 'off')")

    args = p.parse_args()
    dispatch = {
        "start":  cmd_start,
        "stop":   cmd_stop,
        "status": cmd_status,
        "list":   cmd_list,
        "add":    cmd_add,
        "add-random": cmd_add_random,
        "remove": cmd_remove,
        "image":  cmd_image,
        "skip":   cmd_skip,
        "next":   cmd_next,
        "refresh":cmd_refresh,
    }
    fn = dispatch.get(args.cmd)
    if fn:
        fn(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
