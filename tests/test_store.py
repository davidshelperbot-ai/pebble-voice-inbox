import json
from datetime import datetime, timezone

from voice_inbox import store


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make(tmp_path):
    return store.connect(tmp_path / "q.db")


def test_enqueue_and_due(tmp_path):
    conn = make(tmp_path)
    rid = store.enqueue(conn, "shortcut", "text", "buy milk", None)
    rows = store.due(conn, now())
    assert [r["id"] for r in rows] == [rid]
    assert rows[0]["transcript"] == "buy milk"
    assert rows[0]["status"] == "queued"
    assert rows[0]["attempts"] == 0


def test_audio_row_has_no_transcript(tmp_path):
    conn = make(tmp_path)
    store.enqueue(conn, "device", "audio", None, "/tmp/a.m4a")
    row = store.due(conn, now())[0]
    assert row["transcript"] is None
    assert row["audio_path"] == "/tmp/a.m4a"


def test_record_attempt_defers_row(tmp_path):
    conn = make(tmp_path)
    rid = store.enqueue(conn, "x", "text", "hi", None)
    store.record_attempt(conn, rid, "boom", now(), retry_in_s=60)
    assert store.due(conn, now()) == []          # not due yet
    row = conn.execute("SELECT * FROM commands").fetchone()
    assert row["attempts"] == 1
    assert row["last_error"] == "boom"
    assert row["status"] == "queued"             # still queued for retry


def test_due_returns_deferred_row_after_retry_time(tmp_path):
    conn = make(tmp_path)
    rid = store.enqueue(conn, "x", "text", "hi", None)
    store.record_attempt(conn, rid, "boom", "2020-01-01T00:00:00+00:00", retry_in_s=60)
    assert [r["id"] for r in store.due(conn, now())] == [rid]


def test_mark_dispatched_and_failed_leave_queue(tmp_path):
    conn = make(tmp_path)
    a = store.enqueue(conn, "x", "text", "one", None)
    b = store.enqueue(conn, "x", "text", "two", None)
    store.mark_dispatched(conn, a, now())
    store.mark_failed(conn, b, "gave up")
    assert store.due(conn, now()) == []
    assert store.queue_depth(conn) == 0


def test_enqueue_with_dictated_at(tmp_path):
    conn = make(tmp_path)
    store.enqueue(conn, "shortcut", "text", "buy milk", None,
                  dictated_at="2026-07-30T09:00:00-04:00")
    assert store.due(conn, now())[0]["dictated_at"] == "2026-07-30T09:00:00-04:00"


def test_migration_adds_dictated_at_to_existing_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute(store.SCHEMA.replace(",\n  dictated_at TEXT", ""))
    old.commit()
    old.close()
    conn = store.connect(db)  # must not raise; must add the column
    store.enqueue(conn, "x", "text", "hi", None, dictated_at="t")
    assert store.due(conn, now())[0]["dictated_at"] == "t"


def test_set_transcript(tmp_path):
    conn = make(tmp_path)
    rid = store.enqueue(conn, "device", "audio", None, "/tmp/a.m4a")
    store.set_transcript(conn, rid, "hello world")
    assert store.due(conn, now())[0]["transcript"] == "hello world"


# --- transcript audit log: every ingest is recorded, deterministically ---

def _log_lines(tmp_path):
    p = tmp_path / "transcripts.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_enqueue_appends_received_event_to_transcript_log(tmp_path):
    conn = store.connect(tmp_path / "queue.db")
    rid = store.enqueue(conn, "ring", "text", "buy milk", None,
                        dictated_at="2026-09-10T12:00:00+00:00")
    events = _log_lines(tmp_path)
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "received" and e["id"] == rid
    assert e["transcript"] == "buy milk" and e["source"] == "ring"
    assert e["dictated_at"] == "2026-09-10T12:00:00+00:00"
    assert e["received_at"]


def test_set_transcript_appends_transcribed_event(tmp_path):
    conn = store.connect(tmp_path / "queue.db")
    rid = store.enqueue(conn, "ring", "audio", None, str(tmp_path / "a.m4a"))
    store.set_transcript(conn, rid, "hello there")
    events = _log_lines(tmp_path)
    assert [e["event"] for e in events] == ["received", "transcribed"]
    assert events[1]["transcript"] == "hello there" and events[1]["id"] == rid


def test_transcript_log_is_append_only_across_rows(tmp_path):
    conn = store.connect(tmp_path / "queue.db")
    store.enqueue(conn, "ring", "text", "first", None)
    store.enqueue(conn, "ios", "text", "second", None)
    assert [e["transcript"] for e in _log_lines(tmp_path)] == ["first", "second"]


def test_log_failure_never_breaks_ingest(tmp_path, monkeypatch):
    """An unwritable log must not lose the command — the queue is the source of truth."""
    conn = store.connect(tmp_path / "queue.db")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append_transcript_log", boom)
    rid = store.enqueue(conn, "ring", "text", "still queued", None)
    assert conn.execute("SELECT transcript FROM commands WHERE id=?",
                        (rid,)).fetchone()[0] == "still queued"


# --- audit mirroring bookkeeping ---

def test_pending_audit_lists_rows_with_a_transcript(tmp_path):
    conn = store.connect(tmp_path / "queue.db")
    a = store.enqueue(conn, "ring", "text", "has text", None)
    store.enqueue(conn, "ring", "audio", None, "/tmp/x.m4a")  # no transcript yet
    assert [r["id"] for r in store.pending_audit(conn)] == [a]


def test_mark_audited_removes_row_from_pending(tmp_path):
    conn = store.connect(tmp_path / "queue.db")
    rid = store.enqueue(conn, "ring", "text", "hi", None)
    store.mark_audited(conn, rid, now())
    assert store.pending_audit(conn) == []


def test_audited_at_column_is_added_to_a_preexisting_db(tmp_path):
    import sqlite3
    db = tmp_path / "queue.db"
    legacy = sqlite3.connect(db)
    legacy.execute(store.SCHEMA.replace(",\n  audited_at TEXT", ""))
    legacy.commit()
    legacy.close()
    conn = store.connect(db)
    assert "audited_at" in [r[1] for r in conn.execute("PRAGMA table_info(commands)")]
