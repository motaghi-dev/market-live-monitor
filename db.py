import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any
import json
import datetime as dt


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
  
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            timezone TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_ts TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id INTEGER NOT NULL,
            start_ts TEXT NOT NULL,
            end_ts TEXT NOT NULL,
            audio_path TEXT NOT NULL,
            duration_sec INTEGER NOT NULL,
            created_ts TEXT NOT NULL,
            UNIQUE(stream_id, start_ts, end_ts, audio_path),
            FOREIGN KEY(stream_id) REFERENCES streams(id)
        );

        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id INTEGER NOT NULL UNIQUE,
            text_path TEXT,
            text TEXT,
            model TEXT,
            language TEXT,
            created_ts TEXT NOT NULL,
            FOREIGN KEY(chunk_id) REFERENCES chunks(id)
        );

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id INTEGER NOT NULL UNIQUE,
            summary_json TEXT NOT NULL,
            important INTEGER NOT NULL,
            headline TEXT,
            created_ts TEXT NOT NULL,
            FOREIGN KEY(chunk_id) REFERENCES chunks(id)
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_stream_time ON chunks(stream_id, start_ts);
        CREATE INDEX IF NOT EXISTS idx_summaries_important ON summaries(important);

        -- -------------------------
        -- Entity tagging tables
        -- -------------------------
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical TEXT NOT NULL,      -- e.g. "AAPL" or "Donald Trump" or "United States" or "CPI"
            type TEXT NOT NULL,           -- STOCK / PERSON / COUNTRY / MACRO / ORG
            meta_json TEXT,               -- optional JSON (company name, exchange, etc.)
            UNIQUE(type, canonical)
        );

        CREATE TABLE IF NOT EXISTS entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            UNIQUE(entity_id, alias),
            FOREIGN KEY(entity_id) REFERENCES entities(id)
        );

        CREATE TABLE IF NOT EXISTS chunk_entities (
            chunk_id INTEGER NOT NULL,
            entity_id INTEGER NOT NULL,
            mention TEXT NOT NULL,        -- what we saw in text
            confidence REAL,
            source TEXT,                  -- "stocks", "rules", "macros", "spacy", ...
            PRIMARY KEY(chunk_id, entity_id, mention),
            FOREIGN KEY(chunk_id) REFERENCES chunks(id),
            FOREIGN KEY(entity_id) REFERENCES entities(id)
        );

        CREATE INDEX IF NOT EXISTS idx_chunk_entities_chunk ON chunk_entities(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_chunk_entities_entity ON chunk_entities(entity_id);
        """
    )
    conn.commit()


# -------------------------
# Streams / Chunks / Transcripts / Summaries
# -------------------------
def get_or_create_stream(
    conn: sqlite3.Connection,
    name: str,
    platform: str,
    url: str,
    timezone: Optional[str] = None,
    active: int = 1,
) -> int:
    row = conn.execute("SELECT id FROM streams WHERE url = ?", (url,)).fetchone()
    if row:
        return int(row["id"])

    conn.execute(
        "INSERT INTO streams(name, platform, url, timezone, active, created_ts) VALUES(?,?,?,?,?,?)",
        (name, platform, url, timezone, active, utc_now_iso()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def insert_chunk(
    conn: sqlite3.Connection,
    stream_id: int,
    start_ts: str,
    end_ts: str,
    audio_path: str,
    duration_sec: int,
) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO chunks(stream_id, start_ts, end_ts, audio_path, duration_sec, created_ts)
        VALUES(?,?,?,?,?,?)
        """,
        (stream_id, start_ts, end_ts, audio_path, duration_sec, utc_now_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM chunks WHERE stream_id=? AND start_ts=? AND end_ts=? AND audio_path=?",
        (stream_id, start_ts, end_ts, audio_path),
    ).fetchone()
    return int(row["id"])


def get_chunk_by_audio_path(conn: sqlite3.Connection, audio_path: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM chunks WHERE audio_path = ?", (audio_path,)).fetchone()
    return int(row["id"]) if row else None


def upsert_transcript(
    conn: sqlite3.Connection,
    chunk_id: int,
    text: str,
    text_path: Optional[str] = None,
    model: Optional[str] = None,
    language: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO transcripts(chunk_id, text_path, text, model, language, created_ts)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chunk_id) DO UPDATE SET
            text_path=excluded.text_path,
            text=excluded.text,
            model=excluded.model,
            language=excluded.language
        """,
        (chunk_id, text_path, text, model, language, utc_now_iso()),
    )
    conn.commit()


def upsert_summary(
    conn: sqlite3.Connection,
    chunk_id: int,
    summary: Dict[str, Any],
) -> None:
    important = 1 if bool(summary.get("important")) else 0
    headline = summary.get("headline")
    summary_json = json.dumps(summary, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO summaries(chunk_id, summary_json, important, headline, created_ts)
        VALUES(?,?,?,?,?)
        ON CONFLICT(chunk_id) DO UPDATE SET
            summary_json=excluded.summary_json,
            important=excluded.important,
            headline=excluded.headline
        """,
        (chunk_id, summary_json, important, headline, utc_now_iso()),
    )
    conn.commit()


# -------------------------
# Entities / Aliases / Chunk tagging helpers
# -------------------------
def get_or_create_entity(
    conn: sqlite3.Connection,
    canonical: str,
    type_: str,
    meta: Optional[Dict[str, Any]] = None,
) -> int:
    row = conn.execute(
        "SELECT id FROM entities WHERE type=? AND canonical=?",
        (type_, canonical),
    ).fetchone()
    if row:
        return int(row["id"])

    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
    conn.execute(
        "INSERT INTO entities(canonical, type, meta_json) VALUES(?,?,?)",
        (canonical, type_, meta_json),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_alias(conn: sqlite3.Connection, entity_id: int, alias: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases(entity_id, alias) VALUES(?,?)",
        (entity_id, alias),
    )
    conn.commit()


def find_entity_by_alias(
    conn: sqlite3.Connection,
    type_: str,
    alias: str,
) -> Optional[int]:
    row = conn.execute(
        """
        SELECT e.id
        FROM entity_aliases a
        JOIN entities e ON e.id = a.entity_id
        WHERE e.type=? AND a.alias=?
        """,
        (type_, alias),
    ).fetchone()
    return int(row["id"]) if row else None


def insert_chunk_entity(
    conn: sqlite3.Connection,
    chunk_id: int,
    entity_id: int,
    mention: str,
    confidence: Optional[float],
    source: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO chunk_entities(chunk_id, entity_id, mention, confidence, source)
        VALUES(?,?,?,?,?)
        """,
        (chunk_id, entity_id, mention, confidence, source),
    )
    conn.commit()
