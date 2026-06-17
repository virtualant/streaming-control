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

BASE_DIR      = Path(__file__).parent
CONFIG_FILE   = BASE_DIR / "config.json"
SCHEDULE_FILE = BASE_DIR / "schedule.json"
VIDEOS_DIR    = BASE_DIR / "videos"
BACKGROUND    = BASE_DIR / "background.mp4"
PID_FILE      = BASE_DIR / ".streamer.pid"
LOG_FILE      = BASE_DIR / "streamer.log"
OVERLAY_DIR     = BASE_DIR / ".overlay"
OVERLAY_SLOTS   = 6  # header + 5 schedule lines
PICTURE_OVERLAY = BASE_DIR / "picture-overlay"

DEFAULT_CONFIG = {
    "youtube_rtmp": "rtmp://a.rtmp.youtube.com/live2",
    "stream_key": "YOUR_STREAM_KEY_HERE",
    "bitrate": "4500k",
    "audio_bitrate": "128k",
}


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

def parse_time_arg(time_str):
    """Return (date_str_or_None, 'HH:MM')"""
    time_str = time_str.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(time_str, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            pass
    try:
        datetime.datetime.strptime(time_str, "%H:%M")
        return None, time_str
    except ValueError:
        raise ValueError(
            f"Neispravan format vremena: '{time_str}'. Koristi HH:MM (svaki dan) ili YYYY-MM-DD HH:MM (jednom)."
        )

def item_key(item):
    return (item["title"], item["time"], item.get("date"))

def next_occurrence(item, now):
    h, m = map(int, item["time"].split(":"))
    if item.get("date"):
        return datetime.datetime.strptime(item["date"], "%Y-%m-%d").replace(
            hour=h, minute=m, second=0, microsecond=0
        )
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
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
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
    diff = (now - scheduled).total_seconds()
    return 0 <= diff < 120


# ── Video matching ────────────────────────────────────────────────────────────

def _norm(s):
    return s.lower().replace(" ", "").replace("_", "").replace("-", "").replace(".", "")

def find_video(title):
    """Fuzzy-match title against filenames in videos/. 5-char substring wins."""
    if not VIDEOS_DIR.exists():
        return None
    candidates = list(VIDEOS_DIR.glob("**/*.mp4")) + list(VIDEOS_DIR.glob("**/*.mkv"))
    if not candidates:
        return None

    nt = _norm(title)

    # 5-char substring match (handles typos / partial names)
    for path in candidates:
        nn = _norm(path.stem)
        for i in range(max(0, len(nt) - 4)):
            if nt[i:i+5] in nn:
                return path

    # Fallback: sequence ratio
    best, best_r = None, 0.0
    for path in candidates:
        r = SequenceMatcher(None, nt, _norm(path.stem)).ratio()
        if r > best_r:
            best_r, best = r, path
    return best if best_r > 0.5 else None


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def write_overlay_files(schedule):
    """Write per-slot text files that FFmpeg re-reads every frame via reload=1."""
    OVERLAY_DIR.mkdir(exist_ok=True)
    now = datetime.datetime.now()
    cutoff = now + datetime.timedelta(hours=5)
    upcoming = [
        u for u in get_upcoming(schedule, limit=5, now=now)
        if u["dt"] <= cutoff
    ]

    lines = [f"{u['label']}  —  {u['item']['title']}" for u in upcoming]
    if not lines:
        lines = ["Nema zakazanih videa"]

    # Slot 0 = header, slots 1-5 = schedule lines (blank if unused)
    slots = ["TV RASPORED"] + lines + [""] * (OVERLAY_SLOTS - 1 - len(lines))
    for i, text in enumerate(slots[:OVERLAY_SLOTS]):
        (OVERLAY_DIR / f"line{i}.txt").write_text(text)

def get_picture_overlay():
    """Return the first image (PNG/JPG) found in picture-overlay/, or None."""
    if not PICTURE_OVERLAY.exists():
        return None
    images = sorted([
        p for ext in ("*.png", "*.jpg", "*.jpeg")
        for p in PICTURE_OVERLAY.glob(ext)
    ])
    return images[0] if images else None

def _drawtext_file(slot, x, y, size=30, color="white@0.85"):
    path = str(OVERLAY_DIR / f"line{slot}.txt").replace("\\", "/").replace(":", "\\:")
    return (
        f"drawtext=textfile='{path}':reload=1:"
        f"fontcolor={color}:fontsize={size}:"
        f"x={x}:y={y}:"
        f"shadowcolor=black@0.9:shadowx=2:shadowy=2"
    )

def build_idle_cmd(config, picture=None):
    rtmp = f"{config['youtube_rtmp']}/{config['stream_key']}"

    text_filters = [_drawtext_file(0, 60, 60, size=42, color="white")]
    for i in range(1, OVERLAY_SLOTS):
        text_filters.append(_drawtext_file(i, 60, 60 + i * 58, size=30))

    inputs = ["-re", "-stream_loop", "-1", "-i", str(BACKGROUND)]

    if picture:
        inputs += ["-i", str(picture)]
        text_chain = ",".join(text_filters)
        fc = f"[0:v]{text_chain}[txt];[txt][1:v]overlay=0:0[out]"
        return [
            "ffmpeg", "-hide_banner", *inputs,
            "-filter_complex", fc, "-map", "[out]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", config["bitrate"], "-maxrate", config["bitrate"], "-bufsize", "9000k",
            "-c:a", "aac", "-b:a", config["audio_bitrate"], "-ar", "44100",
            "-f", "flv", rtmp,
        ]

    return [
        "ffmpeg", "-hide_banner", *inputs,
        "-vf", ",".join(text_filters),
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", config["bitrate"], "-maxrate", config["bitrate"], "-bufsize", "9000k",
        "-c:a", "aac", "-b:a", config["audio_bitrate"], "-ar", "44100",
        "-f", "flv", rtmp,
    ]

def is_vertical(video_path):
    """Return True if the video is taller than it is wide."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0", str(video_path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        parts = result.stdout.decode().strip().split(",")
        if len(parts) == 2:
            try:
                w, h = int(parts[0]), int(parts[1])
                return h > w
            except ValueError:
                pass
    return False

# Scales vertical video to fit 1080p with black pillarboxes on both sides
WIDESCREEN_VF = "scale=-2:1080,pad=1920:1080:(ow-iw)/2:0:black"

def build_video_cmd(config, video_path, picture=None):
    rtmp = f"{config['youtube_rtmp']}/{config['stream_key']}"
    vertical = is_vertical(video_path)

    if picture:
        if vertical:
            fc = f"[0:v]{WIDESCREEN_VF}[wide];[wide][1:v]overlay=0:0[out]"
        else:
            fc = "[0:v][1:v]overlay=0:0[out]"
        return [
            "ffmpeg", "-hide_banner",
            "-re", "-i", str(video_path),
            "-i", str(picture),
            "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", config["bitrate"], "-maxrate", config["bitrate"], "-bufsize", "9000k",
            "-c:a", "aac", "-b:a", config["audio_bitrate"], "-ar", "44100",
            "-f", "flv", rtmp,
        ]

    vf = WIDESCREEN_VF if vertical else None
    cmd = [
        "ffmpeg", "-hide_banner",
        "-re", "-i", str(video_path),
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", config["bitrate"], "-maxrate", config["bitrate"], "-bufsize", "9000k",
        "-c:a", "aac", "-b:a", config["audio_bitrate"], "-ar", "44100",
        "-f", "flv", rtmp,
    ]
    return cmd

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
        self.config = config
        self.proc = None
        self.mode = None          # "idle" | "video"
        self.playing_key = None   # item_key() of current video
        self.current_picture = None
        self.running = True
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def _shutdown(self, *_):
        log.info("Shutting down")
        self.running = False
        kill_ffmpeg(self.proc)
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    def _idle(self):
        kill_ffmpeg(self.proc)
        schedule = load_schedule()
        write_overlay_files(schedule)
        picture = get_picture_overlay()
        cmd = build_idle_cmd(self.config, picture)
        log.info(f"Idle stream started  (slika: {picture.name if picture else 'nema'})")
        self.proc = start_ffmpeg(cmd)
        self.mode = "idle"
        self.playing_key = None
        self.current_picture = picture

    def _play(self, item, video_path):
        kill_ffmpeg(self.proc)
        picture = get_picture_overlay()
        cmd = build_video_cmd(self.config, video_path, picture)
        log.info(f"Playing: {item['title']}  ({video_path.name})  (slika: {picture.name if picture else 'nema'})")
        self.proc = start_ffmpeg(cmd)
        self.mode = "video"
        self.playing_key = item_key(item)
        self.current_picture = picture

    def run(self):
        PID_FILE.write_text(str(os.getpid()))
        log.info(f"Streamer daemon started  PID={os.getpid()}")
        PICTURE_OVERLAY.mkdir(exist_ok=True)
        self._idle()

        while self.running:
            time.sleep(5)
            now = datetime.datetime.now()
            schedule = load_schedule()

            # Always keep overlay files current — FFmpeg reads them live via reload=1
            if self.mode == "idle":
                write_overlay_files(schedule)

            # Restart stream if picture overlay changed (added, removed, or replaced)
            picture = get_picture_overlay()
            if picture != self.current_picture:
                log.info(f"Promjena slike overlay-a: {self.current_picture} → {picture}")
                if self.mode == "idle":
                    self._idle()
                elif self.mode == "video":
                    # Rebuild video cmd with new picture — need to know which video is playing
                    # Re-derive video path from playing_key
                    playing_title = self.playing_key[0] if self.playing_key else None
                    video_path = find_video(playing_title) if playing_title else None
                    if video_path:
                        self._play({"title": playing_title, "time": self.playing_key[1], "date": self.playing_key[2]}, video_path)
                    else:
                        self._idle()
                continue

            # Handle unexpected FFmpeg exit
            if self.proc and self.proc.poll() is not None:
                if self.mode == "video":
                    log.info(f"Video finished: {self.playing_key}")
                    # Remove consumed one-time entries
                    schedule = [
                        s for s in schedule
                        if not (item_key(s) == self.playing_key and s.get("date"))
                    ]
                    save_schedule(schedule)
                else:
                    log.warning("Idle stream exited unexpectedly — restarting")
                self._idle()
                continue

            # Check schedule
            for item in schedule:
                if should_play_now(item, now):
                    key = item_key(item)
                    if self.mode == "video" and self.playing_key == key:
                        break  # already playing this
                    video = find_video(item["title"])
                    if video:
                        self._play(item, video)
                    else:
                        log.warning(f"No video file found for: {item['title']}")
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


def cmd_add(args):
    date_str, time_str = parse_time_arg(args.time)
    schedule = load_schedule()
    entry = {"title": args.title, "time": time_str, "date": date_str}
    schedule.append(entry)
    save_schedule(schedule)
    if date_str:
        print(f"Dodano (jednom):    '{args.title}'  dana {date_str} u {time_str}")
    else:
        print(f"Dodano (svaki dan): '{args.title}'  svaki dan u {time_str}")


def cmd_list(_args):
    schedule = load_schedule()
    if not schedule:
        print("Raspored je prazan.")
        return
    now = datetime.datetime.now()
    upcoming_map = {item_key(u["item"]): u["label"] for u in get_upcoming(schedule, limit=100, now=now)}
    print(f"\n {'ID':>3}   {'Sljedeće':^18}   {'Ponavljanje':^11}   Naziv")
    print("  " + "─" * 60)
    for i, item in enumerate(schedule):
        when = upcoming_map.get(item_key(item), item.get("date", "?"))
        repeat = "svaki dan" if not item.get("date") else "jednom"
        print(f"  {i:>3}   {when:<18}   {repeat:<11}   {item['title']}")
    print()


def cmd_remove(args):
    schedule = load_schedule()
    if args.id < 0 or args.id >= len(schedule):
        print(f"Ne postoji unos s ID-em {args.id}  (pokreni 'stream list' za prikaz ID-eva)")
        return
    removed = schedule.pop(args.id)
    save_schedule(schedule)
    print(f"Uklonjeno: '{removed['title']}'")


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

    pa = sub.add_parser("add", help="Dodaj zakazani stream")
    pa.add_argument("title", help="Naziv (uspoređuje se s datotekama u videos/)")
    pa.add_argument("time",  help="HH:MM  ili  YYYY-MM-DD HH:MM")

    pr = sub.add_parser("remove", help="Ukloni zakazani stream prema ID-u")
    pr.add_argument("id", type=int, help="ID iz 'stream list'")

    args = p.parse_args()
    dispatch = {
        "start":  cmd_start,
        "stop":   cmd_stop,
        "status": cmd_status,
        "list":   cmd_list,
        "add":    cmd_add,
        "remove": cmd_remove,
    }
    fn = dispatch.get(args.cmd)
    if fn:
        fn(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
