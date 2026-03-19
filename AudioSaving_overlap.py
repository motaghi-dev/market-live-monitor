import shutil
import subprocess
import signal
import time
import sys
import datetime as dt
from pathlib import Path
from typing import Optional

from db import connect, init_db, get_or_create_stream, insert_chunk

# -------------------------
# Config
# -------------------------
YOUTUBE_URL = "https://www.youtube.com/watch?v=KQp-e_XQnDE"

OUT_DIR = Path("chunks")
LOG_DIR = Path("logs")
DB_PATH = Path("algoalps.db")

COOKIES_FILE = Path("cookies.txt")
PLAYER_CLIENT = "android"

# Sliding window params
DURATION_SEC = 300
OVERLAP_SEC = 5
STEP_SEC = DURATION_SEC - OVERLAP_SEC  


AUDIO_SR = "16000"
AUDIO_CHANNELS = "1"
AUDIO_BITRATE = "48k"

STREAM_NAME = "Yahoo Finance 24/7"
STREAM_PLATFORM = "youtube"
STREAM_TIMEZONE = "America/Chicago"

BACKOFF_START = 3
BACKOFF_MAX = 60


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def ensure_tools():
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("Missing 'ffmpeg'. Install it and ensure it's on PATH.")

    ytdlp_ok = shutil.which("yt-dlp") is not None
    if not ytdlp_ok:
        try:
            import yt_dlp 
            ytdlp_ok = True
        except Exception:
            ytdlp_ok = False

    if not ytdlp_ok:
        raise RuntimeError(
            "Missing 'yt-dlp'. Install it (python -m pip install -U yt-dlp) "
            "or ensure yt-dlp is on PATH."
        )

    if not COOKIES_FILE.exists():
        raise RuntimeError("Missing cookies.txt in project folder.")


def ytdlp_base_cmd():
    if shutil.which("yt-dlp") is not None:
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def record_one_chunk(out_file: Path) -> None:
    """
    yt-dlp -> ffmpeg
    - yt-dlp writes bestaudio bytes to stdout
    - ffmpeg reads from stdin, transcodes to mp3 for exactly DURATION_SEC
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    ytdlp_cmd = ytdlp_base_cmd() + [
        "--cookies", str(COOKIES_FILE),
        "--extractor-args", f"youtube:player_client={PLAYER_CLIENT}",
        "--no-playlist",
        "-f", "bestaudio/best",
        "-o", "-",
        YOUTUBE_URL,
    ]

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-i", "pipe:0",
        "-t", str(DURATION_SEC),    
        "-vn",
        "-ac", AUDIO_CHANNELS,
        "-ar", AUDIO_SR,
        "-b:a", AUDIO_BITRATE,
        str(out_file),
    ]

    ytdlp_log_f = open(str(LOG_DIR / "ytdlp.log"), "ab", buffering=0)
    ffmpeg_log_f = open(str(LOG_DIR / "ffmpeg.log"), "ab", buffering=0)

    ytdlp = subprocess.Popen(ytdlp_cmd, stdout=subprocess.PIPE, stderr=ytdlp_log_f)
    ffmpeg = subprocess.Popen(ffmpeg_cmd, stdin=ytdlp.stdout, stderr=ffmpeg_log_f)
    ytdlp.stdout.close()

    try:
        rc = ffmpeg.wait()
     
        if rc != 0:
            raise RuntimeError(f"ffmpeg exited with code {rc}")
    finally:
        for p in (ffmpeg, ytdlp):
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        time.sleep(0.5)
        for p in (ffmpeg, ytdlp):
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        try:
            ytdlp_log_f.close()
        except Exception:
            pass
        try:
            ffmpeg_log_f.close()
        except Exception:
            pass


def main():
    ensure_tools()

    conn = connect(DB_PATH)
    init_db(conn)
    stream_id = get_or_create_stream(
        conn,
        name=STREAM_NAME,
        platform=STREAM_PLATFORM,
        url=YOUTUBE_URL,
        timezone=STREAM_TIMEZONE,
        active=1,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    stop_flag = {"stop": False}

    def handle_stop(sig, frame):
        stop_flag["stop"] = True
        print("\nStopping…", flush=True)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    backoff = BACKOFF_START
    chunk_index = 0

    print("Sliding window: duration=", DURATION_SEC, "step=", STEP_SEC, "overlap=", OVERLAP_SEC)
    print("Writing to:", OUT_DIR.resolve())
    print("DB:", DB_PATH.resolve())

    while not stop_flag["stop"]:
        # Create deterministic filenames
        out_file = OUT_DIR / f"chunk_{chunk_index:05d}.mp3"

        start_epoch = time.time()
        start_ts = utc_now_iso()

        try:
            print("Recording:", out_file.name, "start:", start_ts, flush=True)
            record_one_chunk(out_file)

            end_epoch = time.time()
            end_ts = utc_now_iso()

            # Insert into DB
            insert_chunk(
                conn,
                stream_id=stream_id,
                start_ts=start_ts,
                end_ts=end_ts,
                audio_path=str(out_file.resolve()),
                duration_sec=DURATION_SEC,
            )

            print("Saved:", out_file.name, "end:", end_ts, flush=True)

     
            elapsed = end_epoch - start_epoch
            sleep_for = max(0.0, STEP_SEC - elapsed)
            if sleep_for > 0:
                time.sleep(sleep_for)

            backoff = BACKOFF_START
            chunk_index += 1

        except Exception as e:
            print("Error:", repr(e), flush=True)
            print("Check logs in:", LOG_DIR.resolve(), flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
