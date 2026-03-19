import time
import json
from pathlib import Path

from db import connect, init_db, get_chunk_by_audio_path, upsert_transcript

# -------------------------
# Config
# -------------------------
CHUNKS_DIR = Path("chunks")
TRANSCRIPTS_DIR = Path("transcripts")
SEGMENTS_DIR = Path("transcripts_segments")  
DB_PATH = Path("algoalps.db")

MODEL_SIZE = "small"   
LANGUAGE = "en"       
DEVICE = "cpu"       
COMPUTE_TYPE = "int8" 

POLL_SECONDS = 2
STABLE_SECONDS = 3    

SAVE_SEGMENTS_JSON = True  


VAD_FILTER = False
VAD_MIN_SILENCE_MS = 500


def is_file_stable(path: Path, stable_seconds: int) -> bool:
    """Return True if file size hasn't changed for stable_seconds."""
    try:
        size1 = path.stat().st_size
    except FileNotFoundError:
        return False
    time.sleep(stable_seconds)
    try:
        size2 = path.stat().st_size
    except FileNotFoundError:
        return False
    return size1 == size2 and size2 > 0


def main():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit(
            "Missing faster-whisper.\n"
            "Install with:\n"
            "  pip install -U faster-whisper\n"
            "Also ensure you have FFmpeg installed.\n"
        )

    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    if SAVE_SEGMENTS_JSON:
        SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    # DB init
    conn = connect(DB_PATH)
    init_db(conn)

    # Load model once
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)

    # Track already processed chunks by transcript existence
    done = set(p.stem for p in TRANSCRIPTS_DIR.glob("chunk_*.txt"))

    print("Watching:", CHUNKS_DIR.resolve())
    print("Saving transcripts to:", TRANSCRIPTS_DIR.resolve())
    print("DB:", DB_PATH.resolve())
    print("Model:", MODEL_SIZE, "| device:", DEVICE, "| compute:", COMPUTE_TYPE)

    while True:
        for audio_path in sorted(CHUNKS_DIR.glob("chunk_*.mp3")):
            stem = audio_path.stem
            if stem in done:
                continue

            if not is_file_stable(audio_path, STABLE_SECONDS):
                continue

            out_txt = TRANSCRIPTS_DIR / f"{stem}.txt"
            out_json = SEGMENTS_DIR / f"{stem}.json"

            try:
                print("Transcribing:", audio_path.name)

                transcribe_kwargs = dict(
                    language=LANGUAGE,
                    beam_size=5,
                )
                if VAD_FILTER:
                    transcribe_kwargs["vad_filter"] = True
                    transcribe_kwargs["vad_parameters"] = {"min_silence_duration_ms": VAD_MIN_SILENCE_MS}
                else:
                    transcribe_kwargs["vad_filter"] = False

                segments, info = model.transcribe(str(audio_path), **transcribe_kwargs)

                # Collect output
                full_text_parts = []
                seg_rows = []
                for seg in segments:
                    text = (seg.text or "").strip()
                    if text:
                        full_text_parts.append(text)
                    seg_rows.append(
                        {
                            "start": float(seg.start),
                            "end": float(seg.end),
                            "text": seg.text,
                        }
                    )

                full_text = " ".join(full_text_parts).strip()
                out_txt.write_text(full_text, encoding="utf-8")

                if SAVE_SEGMENTS_JSON:
                    payload = {
                        "file": audio_path.name,
                        "language": getattr(info, "language", None),
                        "language_probability": getattr(info, "language_probability", None),
                        "duration": getattr(info, "duration", None),
                        "segments": seg_rows,
                    }
                    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

                # Link transcript 
                chunk_id = get_chunk_by_audio_path(conn, str(audio_path.resolve()))
                if chunk_id is not None:
                    upsert_transcript(
                        conn,
                        chunk_id=chunk_id,
                        text=full_text,
                        text_path=str(out_txt.resolve()),
                        model=f"faster-whisper:{MODEL_SIZE}",
                        language=LANGUAGE or getattr(info, "language", None),
                    )
                else:
                    print("DB: No chunk row found yet for", audio_path.name, "(will still keep .txt)")

                done.add(stem)
                print("Saved:", out_txt.name, f"({len(full_text)} chars)")

                if len(full_text) == 0:
                    print("Note: transcript is empty. Try VAD_FILTER=False (current) or verify audio has speech.")

            except Exception as e:
                print("Failed:", audio_path.name, "error:", repr(e))

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
