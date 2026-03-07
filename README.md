# AlgoAlps Live Monitor

This repo is a simple pipeline for monitoring a live financial stream.

It does four main things:

1) **Capture audio** from a livestream (YouTube right now) into chunked MP3 files  
2) **Transcribe** each chunk locally using **faster-whisper**  
3) **Summarize** each transcript using the **OpenRouter** API (optional)  
4) **Tag entities** (stocks, people, countries, macro topics) using rule-based matching, and store everything in **SQLite**

It’s built for “run it on a Windows machine and let it keep going”.

---

## What’s in this repo

### Python scripts (project root)

- **AudioSaving.py**  
  Records a livestream and writes **non-overlapping** MP3 chunks into `./chunks/`.  
  It also inserts a row in the SQLite DB for each chunk.

- **AudioSaving_overlap.py**  
  Alternative recorder that’s meant to create a **sliding window** (duration + step) so you can get a small overlap between chunks.  
  Note: overlap in a live, restarted pipeline is **approximate** because the next recording can only start after the previous one finishes and the processes restart.  
  If you need “exact 5 seconds overlap”, the clean way is to keep non-overlapping audio files and create overlap at the *transcription stage* (prepend the last few seconds of the previous transcript/audio).

- **transcribe_faster_whisper.py**  
  Watches `./chunks/` for new `chunk_*.mp3` files.  
  When a file stops growing (stable size), it transcribes it with **faster-whisper** and writes:
  - `./transcripts/chunk_XXXXX.txt`
  - `./transcripts_segments/chunk_XXXXX.json` (timestamps + segments)

  It also updates the SQLite DB with the transcript text.

- **summarize_openrouter.py** (optional)  
  Watches `./transcripts/` for new `chunk_*.txt` files.  
  Sends each chunk to OpenRouter and writes JSON summaries to `./summaries/`.  
  It also updates the SQLite DB with the summary JSON and a boolean `important` flag.

  **Important:** your current file contains an API key hard-coded. Do not commit that to GitHub.  
  Use an environment variable instead (instructions below). If that key was shared, rotate it.

- **tag_entities_rules.py**  
  Rule-based entity tagging.
  - Loads `stocks_universe.csv`
  - Uses simple alias lists (example: “Trump” → “Donald Trump”)  
  - Uses regex rules for macro topics (CPI, inflation, Fed, etc.)
  - Reads transcripts from the DB and writes tags to DB tables (`entities`, `entity_aliases`, `chunk_entities`)

- **db.py**  
  The SQLite “data access layer”.  
  It defines the DB schema (tables) and helper functions:
  - create / connect DB
  - insert chunk rows
  - upsert transcript rows
  - upsert summary rows
  - store entity tables + tagging relationships

- **init_db.py**  
  One-time DB initializer.  
  This script is optional because the other scripts call `init_db()` when they connect.  
  Also: this file currently uses an absolute Windows path; change it to a relative path before pushing to GitHub.

### Data files

- **stocks_universe.csv**  
  Your stock “universe” file.

  Columns:
  - `ticker` (ex: `AAPL`)
  - `name` (ex: `Apple Inc.`)
  - `aliases` (pipe-separated; ex: `Apple|Apple Inc|AAPL`)

  Example row:
  ```csv
  ticker,name,aliases
  AAPL,Apple Inc.,Apple|Apple Inc|AAPL
  ```

### Output folders created at runtime

These are created automatically when scripts run:

- `chunks/` — MP3 chunks from the livestream  
- `transcripts/` — plain text transcripts  
- `transcripts_segments/` — JSON with per-segment timestamps  
- `summaries/` — JSON summaries (optional)  
- `logs/` — ffmpeg/yt-dlp log files  
- `algoalps.db` — SQLite DB file (in repo root)

---

## The database schema (SQLite)

All structured data ends up in `algoalps.db`.

### Core tables

- **streams**  
  One row per monitored stream (name/platform/url).

- **chunks**  
  One row per saved MP3 chunk.  
  Stores:
  - stream_id
  - start_ts / end_ts (UTC ISO)
  - audio_path (absolute path stored as text)
  - duration_sec

- **transcripts**  
  One row per chunk (same chunk_id).  
  Stores:
  - transcript text
  - model name
  - language

- **summaries**  
  One row per chunk (same chunk_id).  
  Stores:
  - JSON summary text
  - `important` boolean
  - headline

### Entity tagging tables

- **entities**  
  Canonical entities (ex: `AAPL`, `Donald Trump`, `United States`, `CPI`) with a `type`:
  - `STOCK`, `PERSON`, `COUNTRY`, `MACRO`, `ORG`

- **entity_aliases**  
  Alternative names for entities (ex: “Trump”, “President Trump” → `Donald Trump`).

- **chunk_entities**  
  A many-to-many table linking chunks to entities.  
  It stores what mention was seen, a confidence number, and which rule/source triggered it.

### Example records

Example: one chunk gets created

- `chunks` row:
  - stream_id: `1`
  - start_ts: `2026-03-05T13:57:07Z`
  - end_ts: `2026-03-05T14:02:07Z`
  - audio_path: `D:\AlgoAlps\chunks\chunk_00000.mp3`
  - duration_sec: `300`

Then after transcription, you get:

- `transcripts` row:
  - chunk_id: `123`
  - model: `small`
  - text: `"…"`

Then tagging might add:

- `entities` row:
  - canonical: `AAPL`
  - type: `STOCK`

- `chunk_entities` row:
  - chunk_id: `123`
  - entity_id: (AAPL id)
  - mention: `Apple`
  - source: `stocks`
  - confidence: `0.95`

---

## Recommended setup (Windows)

### 1) Install tools

You need these system tools available on your PATH:

- **ffmpeg**
- **yt-dlp**

Quick sanity checks:
```bat
ffmpeg -version
yt-dlp --version
```

### 2) Use Python 3.11 (recommended)

Some packages in this project have issues on very new Python versions (3.14 in particular).  
Python **3.11** is a safe default.

### 3) Create a virtual environment (venv)

A **venv** is just a project-local Python install, so packages don’t conflict with your other projects.

From the repo root:
```bat
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
```

### 4) Install Python packages

```bat
.\.venv\Scripts\python.exe -m pip install -U faster-whisper requests
```

(If you only want capture + transcription, you can skip `requests`.)

### 5) Cookies file (YouTube live)

This project expects a `cookies.txt` file in **Netscape cookies format** next to the scripts.

- Put `cookies.txt` in the repo root
- Do **not** commit it to GitHub

Why this exists: YouTube livestream HLS often fails with `403 Forbidden` unless yt-dlp has cookies.

---

## How to run the pipeline

Run each step in its own terminal.  
All scripts assume you run them **from the repo root** (so relative paths work).

### Step 0 (optional): initialize the DB

You can skip this because the other scripts initialize automatically.  
But if you want to create the DB first:

```bat
.\.venv\Scripts\python.exe init_db.py
```

### Step 1: start audio capture

Pick one:

**A) Non-overlapping chunks**
```bat
.\.venv\Scripts\python.exe AudioSaving.py
```

**B) Sliding-window version (overlap intent)**
```bat
.\.venv\Scripts\python.exe AudioSaving_overlap.py
```

This creates `chunks/chunk_00000.mp3`, `chunks/chunk_00001.mp3`, … and DB rows.

Stop it with `Ctrl + C`.

### Step 2: transcribe chunks locally

In a second terminal:
```bat
.\.venv\Scripts\python.exe transcribe_faster_whisper.py
```

This creates:
- `transcripts/chunk_00000.txt`
- `transcripts_segments/chunk_00000.json`

And writes transcripts into the DB.

### Step 3 (optional): summarize via OpenRouter

In a third terminal:
```bat
set OPENROUTER_API_KEY=YOUR_KEY_HERE
.\.venv\Scripts\python.exe summarize_openrouter.py
```

This creates:
- `summaries/chunk_00000.json`
And writes summaries into the DB.

### Step 4: tag entities

Run whenever you want (it can be re-run; duplicates are ignored):
```bat
.\.venv\Scripts\python.exe tag_entities_rules.py
```

---

## Configuration knobs

### Audio chunking

In `AudioSaving.py`:
- `YOUTUBE_URL`
- `CHUNK_SECONDS` (chunk duration)
- audio format settings (SR, channels, bitrate)

In `AudioSaving_overlap.py`:
- `DURATION_SEC` and `STEP_SEC`
- `YOUTUBE_URL`
- same audio format settings

### Transcription

In `transcribe_faster_whisper.py`:
- `MODEL_SIZE`: `tiny`, `base`, `small`, `medium`, `large-v3`
- `DEVICE`: `cpu` or `cuda`
- `COMPUTE_TYPE`:
  - CPU: `int8` is usually best
  - CUDA: `float16` is typical

### Summaries

In `summarize_openrouter.py`:
- `MODEL` (OpenRouter model string)
- `SYSTEM_PROMPT` (controls the JSON format and what “important” means)
- `MAX_CONTEXT_CHUNKS` (reduces repetition)

### Stock universe

In `stocks_universe.csv`:
- Add the full S&P 500 list if that’s your goal.
- Keep aliases clean and consistent.
- Use `|` between aliases.

---

## Useful SQL queries (examples)

Open SQLite with any DB browser, or use the CLI.

### Latest important summaries
```sql
SELECT c.start_ts, s.headline, s.summary_json
FROM summaries s
JOIN chunks c ON c.id = s.chunk_id
WHERE s.important = 1
ORDER BY c.start_ts DESC
LIMIT 20;
```

### Find chunks that mentioned a ticker
```sql
SELECT c.start_ts, e.canonical, ce.mention, ce.source
FROM chunk_entities ce
JOIN entities e ON e.id = ce.entity_id
JOIN chunks c ON c.id = ce.chunk_id
WHERE e.type = 'STOCK' AND e.canonical = 'AAPL'
ORDER BY c.start_ts DESC
LIMIT 50;
```

## What’s not done yet (roadmap)

These are the next steps that were discussed:

- Schedule-based monitoring (only record during key show times)
- Support multiple streams (CNBC + other platforms)
- More robust “uniform tags” (better alias normalization)
- Combine per-chunk JSON outputs into one `.pkl` file for batch analysis
