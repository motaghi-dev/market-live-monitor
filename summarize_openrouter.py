import os
import time
import json
from pathlib import Path
import requests

from db import connect, init_db, get_chunk_by_audio_path, upsert_summary

# ---- Config ----
TRANSCRIPTS_DIR = Path("transcripts")
SUMMARIES_DIR = Path("summaries")
DB_PATH = Path("algoalps.db")

MODEL = "arcee-ai/trinity-large-preview:free"
POLL_SECONDS = 2
MAX_CONTEXT_CHUNKS = 5  # include last N summaries as context to reduce repetition

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise RuntimeError(
        "OPENROUTER_API_KEY is not set.\n"
        "Windows CMD (temporary):  set OPENROUTER_API_KEY=...\n"
        "Windows CMD (permanent):  setx OPENROUTER_API_KEY \"...\"  (then reopen CMD)\n"
    )

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}

SYSTEM_PROMPT = """You are monitoring a live broadcast transcript (5-minute chunks).
Your job: produce a short summary and decide if anything IMPORTANT happened.

IMPORTANT means: new announcement, breaking news, new numbers/dates, policy/legal claim,
market-moving claim, major accusation, correction/recantation, direct call-to-action,
or anything that materially changes the narrative.

Return ONLY valid JSON with keys:
{
  "important": true/false,
  "headline": "one short headline",
  "bullets": ["...", "...", "..."],
  "why_important": "one sentence",
  "key_quotes": ["quote1", "quote2"]
}
Keep bullets concise (max 4). If no good quotes, return [].
"""

def call_openrouter(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        OPENROUTER_URL,
        headers=DEFAULT_HEADERS,
        data=json.dumps(payload),
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

def stem_to_audio_path(stem: str) -> Path:
    # Your transcript filenames are chunk_00000.txt, so the audio should be chunks/chunk_00000.mp3
    return Path("chunks") / f"{stem}.mp3"

def main():
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect(DB_PATH)
    init_db(conn)

    done = set(p.stem for p in SUMMARIES_DIR.glob("chunk_*.json"))
    recent_summaries = []

    print("Watching:", TRANSCRIPTS_DIR.resolve())
    print("Saving summaries to:", SUMMARIES_DIR.resolve())
    print("DB:", DB_PATH.resolve())
    print("Model:", MODEL)

    while True:
        for txt_path in sorted(TRANSCRIPTS_DIR.glob("chunk_*.txt")):
            stem = txt_path.stem
            if stem in done:
                continue

            transcript = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
            if not transcript:
                continue

            # Build context from recent summaries
            context_block = "\n".join(f"- {s}" for s in recent_summaries[-MAX_CONTEXT_CHUNKS:]) or "(none)"

            user_prompt = f"""Recent context (previous chunk summaries):
{context_block}

Current 5-minute transcript ({txt_path.name}):
{transcript}
"""

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            try:
                print("Summarizing:", txt_path.name)
                content = call_openrouter(messages)

                # Validate JSON
                result = json.loads(content)

                # Write JSON file
                out_json = SUMMARIES_DIR / f"{stem}.json"
                out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

                # Upsert to DB (link by audio_path -> chunk_id)
                audio_path = stem_to_audio_path(stem).resolve()
                chunk_id = get_chunk_by_audio_path(conn, str(audio_path))
                if chunk_id is not None:
                    upsert_summary(conn, chunk_id=chunk_id, summary=result)
                else:
                    print("DB: No chunk row found yet for", audio_path.name, "(kept JSON file)")

                # Update rolling context
                headline = result.get("headline", "")
                bullets = result.get("bullets", [])
                recent_summaries.append(headline + " | " + " ".join(bullets[:3]))

                done.add(stem)
                print("Saved:", out_json.name, "| important:", result.get("important", False))

            except Exception as e:
                print("Failed:", txt_path.name, "error:", repr(e))
                time.sleep(5)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
