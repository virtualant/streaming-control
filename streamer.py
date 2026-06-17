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
OVERLAY_DIR         = BASE_DIR / ".overlay"
OVERLAY_SLOTS       = 6  # header + 5 schedule lines
PICTURE_OVERLAY     = BASE_DIR / "picture-overlay"
SELECTED_IMAGE_FILE = BASE_DIR / ".selected_image"
FIFO_PATH           = BASE_DIR / ".pip.fifo"

# PIP box: desna strana ekrana, ne prekriva overlay tekst lijevo
PIP_W, PIP_H = 1240, 698   # 16:9
PIP_X, PIP_Y = 640,  191   # x=640 ostavlja 580px za overlay, y centriran

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

    # "today HH:MM" → današnji datum
    if time_str.lower().startswith("today "):
        hm = time_str[6:].strip()
        try:
            datetime.datetime.strptime(hm, "%H:%M")
            return datetime.date.today().isoformat(), hm
        except ValueError:
            pass

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
            f"Neispravan format vremena: '{time_str}'. Koristi HH:MM (svaki dan), today HH:MM ili YYYY-MM-DD HH:MM (jednom)."
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

    def fmt(u):
        title = u['item'].get('display_title') or u['item']['title']
        if len(title) > 40:
            title = title[:38] + ".."
        return f"{u['label']}  {title}"

    lines = [fmt(u) for u in upcoming]
    # Slot 0 = header, slots 1-5 = schedule lines (blank if unused)
    slots = ["TSETSE TV"] + lines + [""] * (OVERLAY_SLOTS - 1 - len(lines))
    for i, text in enumerate(slots[:OVERLAY_SLOTS]):
        (OVERLAY_DIR / f"line{i}.txt").write_text(text)

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

def build_feeder_cmd(config, picture=None, seek=0.0):
    """Permanent compositor: background + PIP FIFO (+ optional picture) → nginx."""
    seek_args = ["-ss", f"{seek:.2f}"] if seek > 0 else []

    text_filters = [_drawtext_file(0, 60, 60, size=42, color="white")]
    for i in range(1, OVERLAY_SLOTS):
        text_filters.append(_drawtext_file(i, 60, 60 + i * 58, size=28))
    text_chain = ",".join(text_filters)

    pip_input = [
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-video_size", f"{PIP_W}x{PIP_H}", "-framerate", "30",
        "-i", str(FIFO_PATH),
    ]

    inputs = [*seek_args, "-re", "-stream_loop", "-1", "-i", str(BACKGROUND), *pip_input]

    if picture:
        inputs += ["-loop", "1", "-i", str(picture)]
        fc = (
            f"[0:v]{text_chain}[bg];"
            f"[bg][1:v]overlay={PIP_X}:{PIP_Y}[pip];"
            f"[pip][2:v]overlay=(W-w)/2:(H-h)/2[out]"
        )
    else:
        fc = (
            f"[0:v]{text_chain}[bg];"
            f"[bg][1:v]overlay={PIP_X}:{PIP_Y}[out]"
        )

    return [
        "ffmpeg", "-hide_banner", *inputs,
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", config["bitrate"], "-maxrate", config["bitrate"], "-bufsize", "9000k",
        "-c:a", "aac", "-b:a", config["audio_bitrate"], "-ar", "44100",
        "-f", "flv", "rtmp://localhost/live/feed",
    ]

def build_pip_black_cmd():
    """Writes black frames into the PIP FIFO when no video is scheduled."""
    return [
        "ffmpeg", "-hide_banner",
        "-f", "lavfi", "-i", f"color=black:size={PIP_W}x{PIP_H}:rate=30",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        str(FIFO_PATH),
    ]

def build_pip_video_cmd(video_path):
    """Scales video to PIP box and writes raw frames into the FIFO."""
    vf = (
        f"scale={PIP_W}:{PIP_H}:force_original_aspect_ratio=decrease,"
        f"pad={PIP_W}:{PIP_H}:(ow-iw)/2:(oh-ih)/2:black"
    )
    return [
        "ffmpeg", "-hide_banner",
        "-re", "-i", str(video_path),
        "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        str(FIFO_PATH),
    ]

def build_main_cmd(config):
    """Pulls composed stream from nginx and copies to YouTube — zero re-encode."""
    rtmp = f"{config['youtube_rtmp']}/{config['stream_key']}"
    return [
        "ffmpeg", "-hide_banner",
        "-i", "rtmp://localhost/live/feed",
        "-c", "copy",
        "-f", "flv", rtmp,
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
        self.config      = config
        self.main_proc   = None   # nginx → YouTube (-c copy, nikad ne staje)
        self.feeder_proc = None   # background + PIP FIFO → nginx (rijetko staje)
        self.pip_proc    = None   # crno ili video → FIFO (swapa se)
        self.fifo_fd     = None   # drži write-end FIFO otvorenim (sprječava EOF)
        self.mode        = None   # "idle" | "video"
        self.playing_key = None
        self.current_picture = None
        self.played_today    = {}
        self.feeder_started_at = 0.0
        self.bg_duration = 30.0
        self.running = True
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def _shutdown(self, *_):
        log.info("Shutting down")
        self.running = False
        for p in (self.pip_proc, self.feeder_proc, self.main_proc):
            kill_ffmpeg(p)
        if self.fifo_fd is not None:
            try:
                os.close(self.fifo_fd)
            except OSError:
                pass
        FIFO_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    def _seek(self):
        """Pozicija u background loopu da se nastavi od iste točke."""
        if self.feeder_started_at > 0 and self.bg_duration > 0:
            return (time.time() - self.feeder_started_at) % self.bg_duration
        return 0.0

    def _start_feeder(self):
        seek = self._seek()
        kill_ffmpeg(self.feeder_proc)
        picture = get_picture_overlay() if self.mode == "idle" else None
        write_overlay_files(load_schedule())
        cmd = build_feeder_cmd(self.config, picture, seek)
        log.info(f"Feeder pokrenut  seek={seek:.1f}s  slika={picture.name if picture else 'nema'}")
        self.feeder_proc = start_ffmpeg(cmd)
        self.feeder_started_at = time.time() - seek
        self.current_picture = picture

    def _start_pip_black(self):
        kill_ffmpeg(self.pip_proc)
        self.pip_proc = start_ffmpeg(build_pip_black_cmd())
        log.info("PIP: crni ekran")

    def _idle(self):
        self._start_pip_black()
        self.mode = "idle"
        self.playing_key = None
        self._start_feeder()

    def _play(self, item, video_path):
        self.mode = "video"  # postaviti prije _start_feeder da izostavi picture
        self.playing_key = item_key(item)
        today = datetime.date.today().isoformat()
        self.played_today[self.playing_key] = today
        self._start_feeder()   # restart bez picture overlaya
        kill_ffmpeg(self.pip_proc)
        self.pip_proc = start_ffmpeg(build_pip_video_cmd(video_path))
        log.info(f"PIP: video '{item['title']}' ({video_path.name})")

    def run(self):
        PID_FILE.write_text(str(os.getpid()))
        log.info(f"Streamer daemon pokrenut  PID={os.getpid()}")
        PICTURE_OVERLAY.mkdir(exist_ok=True)
        VIDEOS_DIR.mkdir(exist_ok=True)

        # Stvori FIFO i drži write-end otvorenim da feeder nikad ne dobije EOF
        FIFO_PATH.unlink(missing_ok=True)
        os.mkfifo(str(FIFO_PATH))
        self.fifo_fd = os.open(str(FIFO_PATH), os.O_RDWR | os.O_NONBLOCK)

        self.bg_duration = get_bg_duration()

        # Redoslijed pokretanja: pip → feeder → (pauza) → main
        self.mode = "idle"
        self._start_pip_black()
        time.sleep(0.5)
        self._start_feeder()
        time.sleep(3)   # daj feederu vremena da počne pushati u nginx
        self.main_proc = start_ffmpeg(build_main_cmd(self.config))
        log.info("Main proc pokrenut (YouTube pusher)")

        while self.running:
            time.sleep(5)
            now      = datetime.datetime.now()
            schedule = load_schedule()
            today    = datetime.date.today().isoformat()

            write_overlay_files(schedule)

            # Expire played_today
            self.played_today = {k: v for k, v in self.played_today.items() if v == today}

            # Promjena picture overlaya (samo u idle modu) → restart feeder
            if self.mode == "idle":
                picture = get_picture_overlay()
                if picture != self.current_picture:
                    log.info(f"Slika promijenjena → restart feedera")
                    self._start_feeder()
                    continue

            # Feeder health
            if self.feeder_proc and self.feeder_proc.poll() is not None:
                log.warning("Feeder pao, restartanje")
                self._start_feeder()
                continue

            # Main health
            if self.main_proc and self.main_proc.poll() is not None:
                log.warning("Main proc pao, restartanje")
                self.main_proc = start_ffmpeg(build_main_cmd(self.config))
                continue

            # PIP health
            if self.pip_proc and self.pip_proc.poll() is not None:
                if self.mode == "video":
                    log.info(f"Video završio: {self.playing_key}")
                    schedule = [
                        s for s in schedule
                        if not (item_key(s) == self.playing_key and s.get("date"))
                    ]
                    save_schedule(schedule)
                    self._idle()
                else:
                    log.warning("PIP black pao, restartanje")
                    self._start_pip_black()
                continue

            # Provjera rasporeda
            for item in schedule:
                if should_play_now(item, now):
                    key = item_key(item)
                    if self.mode == "video" and self.playing_key == key:
                        break
                    if self.played_today.get(key) == today:
                        break
                    video = find_video(item["title"])
                    if video:
                        self._play(item, video)
                    else:
                        log.warning(f"Video nije pronađen: {item['title']}")
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
    display_title = args.title if not args.title_override else args.title_override
    schedule = load_schedule()
    entry = {"title": args.title, "display_title": display_title, "time": time_str, "date": date_str}
    schedule.append(entry)
    save_schedule(schedule)
    if date_str:
        print(f"Dodano (jednom):    '{display_title}'  dana {date_str} u {time_str}")
    else:
        print(f"Dodano (svaki dan): '{display_title}'  svaki dan u {time_str}")


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
        title = (item.get('display_title') or item['title'])[:40]
        print(f"  {i:>3}   {when:<18}   {repeat:<11}   {title}")
    print()


def cmd_remove(args):
    schedule = load_schedule()
    if args.id < 0 or args.id >= len(schedule):
        print(f"Ne postoji unos s ID-em {args.id}  (pokreni 'stream list' za prikaz ID-eva)")
        return
    removed = schedule.pop(args.id)
    save_schedule(schedule)
    print(f"Uklonjeno: '{removed['title']}'")


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
    pa.add_argument("title", help="Naziv datoteke za pretragu u videos/")
    pa.add_argument("time",  help="HH:MM, today HH:MM ili YYYY-MM-DD HH:MM")
    pa.add_argument("--title", dest="title_override", default=None, metavar="NASLOV",
                    help="Custom naslov koji se prikazuje na streamu")

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
        "remove": cmd_remove,
        "image":  cmd_image,
    }
    fn = dispatch.get(args.cmd)
    if fn:
        fn(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
