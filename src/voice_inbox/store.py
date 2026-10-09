"""SQLite-backed durable command queue.

Rows: queued -> dispatched | failed. Retries stay 'queued' with a future
next_attempt_at so the worker's due() poll naturally re-picks them, including
after a process restart (nothing is ever lost in memory).

Every ingest is also mirrored to an append-only transcripts.jsonl beside the
db. It lives here rather than in the HTTP layer or the triage skill so that no
client and no LLM decision can bypass it — enqueue() is the one door in.
"""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('text','audio')),
  transcript TEXT,
  audio_path TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_attempt_at TEXT NOT NULL,
  dispatched_at TEXT,
  dictated_at TEXT,
  audited_at TEXT
);
"""


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _log_path(conn) -> Path:
    """transcripts.jsonl beside this connection's database file."""
    for _, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main" and filename:
            return Path(filename).parent / "transcripts.jsonl"
    raise ValueError("connection has no on-disk main database")


def _append_transcript_log(conn, event: str, row_id: int, **fields) -> None:
    entry = {"logged_at": _utcnow(), "event": event, "id": row_id, **fields}
    with _log_path(conn).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _log_quietly(conn, event: str, row_id: int, **fields) -> None:
    """The queue is the source of truth; never fail an ingest over its log."""
    try:
        _append_transcript_log(conn, event, row_id, **fields)
    except Exception as exc:
        print(f"[voice-inbox] transcript log write failed ({event} #{row_id}): {exc!r}",
              flush=True)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(commands)")]
    if "dictated_at" not in cols:  # pre-existing DB from before the column existed
        conn.execute("ALTER TABLE commands ADD COLUMN dictated_at TEXT")
    if "audited_at" not in cols:
        conn.execute("ALTER TABLE commands ADD COLUMN audited_at TEXT")
    conn.commit()
    return conn


def enqueue(conn, source: str, kind: str, transcript, audio_path, dictated_at=None) -> int:
    now = _utcnow()
    cur = conn.execute(
        "INSERT INTO commands (received_at, source, kind, transcript, audio_path, next_attempt_at, dictated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (now, source, kind, transcript, audio_path, now, dictated_at),
    )
    conn.commit()
    _log_quietly(conn, "received", cur.lastrowid, received_at=now, source=source,
                 kind=kind, transcript=transcript, audio_path=audio_path,
                 dictated_at=dictated_at)
    return cur.lastrowid


def due(conn, now: str) -> list:
    return conn.execute(
        "SELECT * FROM commands WHERE status='queued' AND next_attempt_at<=? ORDER BY id",
        (now,),
    ).fetchall()


def record_attempt(conn, row_id: int, error: str, now: str, retry_in_s: int) -> None:
    from datetime import datetime, timedelta
    next_at = (datetime.fromisoformat(now) + timedelta(seconds=retry_in_s)).isoformat()
    conn.execute(
        "UPDATE commands SET attempts=attempts+1, last_error=?, next_attempt_at=? WHERE id=?",
        (error, next_at, row_id),
    )
    conn.commit()


def set_transcript(conn, row_id: int, transcript: str) -> None:
    conn.execute("UPDATE commands SET transcript=? WHERE id=?", (transcript, row_id))
    conn.commit()
    _log_quietly(conn, "transcribed", row_id, transcript=transcript)


def pending_audit(conn) -> list:
    """Rows whose transcript exists but has not yet reached the Notion log.

    Independent of dispatch status: the audit trail records everything that was
    said, including commands that later failed to dispatch.
    """
    return conn.execute(
        "SELECT * FROM commands WHERE audited_at IS NULL AND transcript IS NOT NULL"
        " AND TRIM(transcript) <> '' ORDER BY id"
    ).fetchall()


def mark_audited(conn, row_id: int, now: str) -> None:
    conn.execute("UPDATE commands SET audited_at=? WHERE id=?", (now, row_id))
    conn.commit()


def mark_dispatched(conn, row_id: int, now: str) -> None:
    conn.execute(
        "UPDATE commands SET status='dispatched', dispatched_at=? WHERE id=?", (now, row_id)
    )
    conn.commit()


def mark_failed(conn, row_id: int, error: str) -> None:
    conn.execute(
        "UPDATE commands SET status='failed', last_error=? WHERE id=?", (error, row_id)
    )
    conn.commit()


def queue_depth(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM commands WHERE status='queued'").fetchone()[0]
