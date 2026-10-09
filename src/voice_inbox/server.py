"""The HTTP service: accept dictation, queue it durably, route it.

Endpoints
---------
``POST /webhook``  multipart/form-data as the Pebble Index 01 ring sends it
                   (fields ``transcription``, ``audio``, ``recordedAt``,
                   ``client``), or JSON for hand-testing.
``POST /command``  JSON ``{"text", "source", "dictated_at"}``, or a raw audio
                   body -- for iOS Shortcuts and any other client.
``GET  /health``   liveness plus queue depth. No auth.

Both POST endpoints need ``Authorization: Bearer <token>``. The service
refuses to start without a token rather than listening unauthenticated.

Everything is written to a SQLite queue *before* a route is consulted, and a
worker drains it: transcribe audio if needed, then hand the command to the
matching handler. That ordering is the whole point -- speech is unrepeatable,
so it is persisted first and interpreted second. A command whose handler is
broken, or whose integration is down, stays queued and is retried rather than
lost.
"""
import hashlib
import hmac
import json
import os
import re
import threading
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import router, store, transcriber
from .handlers import Command, Result, get as get_handler, load_all

POLL_INTERVAL_S = 2
MAX_ATTEMPTS = 3
MAX_BODY = 50 * 1024 * 1024  # 50 MB audio cap


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_multipart(raw: bytes, ctype: str) -> dict:
    """name -> {"data": bytes, "filename": str|None} for each form-data part."""
    msg = BytesParser(policy=email_default).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
    parts = {}
    if not msg.is_multipart():
        return parts
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            parts[str(name)] = {"data": part.get_payload(decode=True) or b"",
                                "filename": part.get_filename()}
    return parts


def _recorded_at_iso(value: str):
    """Ring sends milliseconds since the Unix epoch; store ISO-8601 UTC."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "queued": store.queue_depth(self.server.conn)})
        else:
            self._reply(404, {"error": "not found"})

    def _authorized(self, allow_ring: bool = False) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        presented = header[7:]
        if hmac.compare_digest(presented, self.server.token):
            return True
        ring = getattr(self.server, "ring_token", "")
        return bool(allow_ring and ring) and hmac.compare_digest(presented, ring)

    def _log_auth_failure(self) -> None:
        """Explain a 401 without ever logging the credential itself."""
        def fp(v: str) -> str:
            return hashlib.sha256(v.encode()).hexdigest()[:8] if v else "-"

        # A token in the wrong header is the common misconfiguration; find it.
        expected_all = {"ring": getattr(self.server, "ring_token", ""),
                        "main": self.server.token}
        misplaced = [
            f"{name}={which}" for name, raw in self.headers.items()
            for which, tok in expected_all.items()
            if tok and name.lower() != "authorization"
            and fp((raw or "").removeprefix("Bearer ").strip()) == fp(tok)
        ]
        if misplaced:
            print(f"[voice-inbox] 401 hint: a valid token was sent in the wrong "
                  f"header: {misplaced} — it must be 'Authorization: Bearer <token>'",
                  flush=True)

        header = self.headers.get("Authorization")
        if header is None:
            sent = [h for h in self.headers.keys() if "auth" in h.lower() or "token" in h.lower()]
            print(f"[voice-inbox] 401: no Authorization header; "
                  f"auth-ish headers seen={sent or 'none'}; "
                  f"all headers={sorted(self.headers.keys())}", flush=True)
            return
        scheme, _, cred = header.partition(" ")
        if not cred:  # bare credential, no scheme
            scheme, cred = "<none>", header
        expected = {"main": self.server.token,
                    "ring": getattr(self.server, "ring_token", "")}
        match = [k for k, v in expected.items() if v and fp(cred) == fp(v)]
        print(f"[voice-inbox] 401 on {self.path}: scheme={scheme!r} "
              f"cred_len={len(cred)} cred_fp={fp(cred)} "
              f"expected ring_fp={fp(expected['ring'])} main_fp={fp(expected['main'])} "
              f"value_matches={match or 'none'} "
              f"has_whitespace={cred != cred.strip()}", flush=True)

    def _save_audio(self, raw: bytes, filename) -> str:
        ext = Path(filename or "a.m4a").suffix or ".m4a"
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
            ext = ".m4a"
        audio_dir = self.server.data_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = audio_dir / f"{stamp}{ext}"
        path.write_bytes(raw)
        return str(path)

    def _handle_webhook(self, raw: bytes, ctype: str) -> None:
        """Pebble Index 01 ring: multipart transcription and/or audio."""
        if ctype == "application/json":  # convenience path for curl testing
            try:
                data = json.loads(raw)
            except Exception:
                self._reply(400, {"error": "bad json"})
                return
            text = (data.get("text") or data.get("transcription") or "").strip()
            if not text:
                self._reply(400, {"error": "empty transcription"})
                return
            rid = store.enqueue(self.server.conn, "ring", "text", text, None,
                                dictated_at=data.get("dictated_at"))
            self._reply(202, {"ok": True, "id": rid})
            return

        parts = _parse_multipart(raw, self.headers.get("Content-Type") or "")
        source = (parts.get("client", {}).get("data", b"ring").decode(
            "utf-8", "replace") or "ring")[:64]
        dictated_at = _recorded_at_iso(
            parts.get("recordedAt", {}).get("data", b"").decode("utf-8", "replace"))
        text = parts.get("transcription", {}).get("data", b"").decode(
            "utf-8", "replace").strip()
        audio = parts.get("audio", {}).get("data", b"")
        audio_path = self._save_audio(
            audio, parts.get("audio", {}).get("filename")) if audio else None
        if text:
            # On-device transcription already ran; keep any audio for reference.
            rid = store.enqueue(self.server.conn, source, "text", text, audio_path,
                                dictated_at=dictated_at)
        elif audio_path:
            rid = store.enqueue(self.server.conn, source, "audio", None, audio_path,
                                dictated_at=dictated_at)
        else:
            self._reply(400, {"error": "no transcription or audio"})
            return
        self._reply(202, {"ok": True, "id": rid})

    def do_POST(self):
        if self.path not in ("/command", "/webhook"):
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized(allow_ring=self.path == "/webhook"):
            self._log_auth_failure()
            self._reply(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > MAX_BODY:
            self._reply(400, {"error": "bad length"})
            return
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if self.path == "/webhook":
            self._handle_webhook(raw, ctype)
            return
        if ctype == "application/json":
            try:
                data = json.loads(raw)
                text = (data.get("text") or "").strip()
                source = str(data.get("source") or "unknown")[:64]
            except Exception:
                self._reply(400, {"error": "bad json"})
                return
            if not text:
                self._reply(400, {"error": "empty text"})
                return
            dictated_at = (str(data.get("dictated_at")) or None) if data.get("dictated_at") else None
            rid = store.enqueue(self.server.conn, source, "text", text, None,
                                dictated_at=dictated_at)
        else:
            source = (self.headers.get("X-Source") or "audio")[:64]
            ext = Path(self.headers.get("X-Filename") or "a.m4a").suffix or ".m4a"
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
                ext = ".m4a"
            audio_dir = self.server.data_dir / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            path = audio_dir / f"{stamp}{ext}"
            path.write_bytes(raw)
            rid = store.enqueue(self.server.conn, source, "audio", None, str(path),
                                dictated_at=self.headers.get("X-Dictated-At"))
        self._reply(202, {"ok": True, "id": rid})

    def log_message(self, fmt, *args):  # quiet default access logging
        print(f"[voice-inbox] {self.address_string()} {fmt % args}", flush=True)


def _backoff_or_fail(conn, row, error: str) -> None:
    """Retry with exponential backoff, then give up and say so."""
    if row["attempts"] + 1 >= MAX_ATTEMPTS:
        store.mark_failed(conn, row["id"], error)
        print(f"[voice-inbox] #{row['id']} failed after {MAX_ATTEMPTS} "
              f"attempts: {error}", flush=True)
    else:
        store.record_attempt(conn, row["id"], error, _utcnow(),
                             retry_in_s=30 * (2 ** row["attempts"]))


def dispatch_row(conn, row, routing: router.Routing) -> None:
    """Hand one command to its handler and record what happened."""
    resolved = routing.resolve(row["transcript"])
    if resolved is None:
        # Nothing claimed it and there is no fallback. Leaving it queued would
        # retry forever, so it is marked failed -- the transcript is still in
        # transcripts.jsonl and the queue for a human to look at.
        store.mark_failed(conn, row["id"], "no route matched and no fallback configured")
        print(f"[voice-inbox] #{row['id']} unrouted: {row['transcript'][:80]}",
              flush=True)
        return

    route, groups = resolved
    cmd = Command(text=row["transcript"], source=row["source"],
                  dictated_at=row["dictated_at"], audio_path=row["audio_path"],
                  groups=groups, config=route.config)
    try:
        result = get_handler(route.handler)(cmd)
    except KeyError as exc:  # unknown handler name: a config error, not transient
        store.mark_failed(conn, row["id"], str(exc))
        print(f"[voice-inbox] #{row['id']} -> {route.name}: {exc}", flush=True)
        return
    except Exception as exc:  # handlers are not asked to be careful
        _backoff_or_fail(conn, row, f"{route.handler} raised {exc!r}")
        return

    if not isinstance(result, Result):
        result = Result.done(str(result or ""))
    if result.ok:
        store.mark_dispatched(conn, row["id"], _utcnow())
        print(f"[voice-inbox] #{row['id']} -> {route.name} ({route.handler}): "
              f"{result.detail[:200]}", flush=True)
    elif result.final:
        store.mark_failed(conn, row["id"], result.detail)
        print(f"[voice-inbox] #{row['id']} -> {route.name} failed: "
              f"{result.detail[:200]}", flush=True)
    else:
        _backoff_or_fail(conn, row, result.detail)


def process_due_once(conn, model_path: Path, routing: router.Routing) -> None:
    for row in store.due(conn, _utcnow()):
        if row["kind"] == "audio" and not row["transcript"]:
            try:
                text = transcriber.transcribe(Path(row["audio_path"]), model_path)
            except transcriber.TranscriptionError as exc:
                _backoff_or_fail(conn, row, str(exc))
                continue
            store.set_transcript(conn, row["id"], text)
            row = conn.execute("SELECT * FROM commands WHERE id=?", (row["id"],)).fetchone()
        dispatch_row(conn, row, routing)


def worker_loop(srv) -> None:
    while not srv.stop_event.is_set():
        try:
            process_due_once(srv.conn, srv.model_path, srv.routing)
        except Exception as exc:
            print(f"[voice-inbox] worker error: {exc!r}", flush=True)
        srv.stop_event.wait(POLL_INTERVAL_S)


def build_server(port: int, token: str, data_dir: Path, model_path: Path,
                 routing: router.Routing | None = None, ring_token: str = "",
                 host: str = "127.0.0.1") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.token = token
    srv.ring_token = ring_token
    srv.data_dir = Path(data_dir)
    srv.model_path = Path(model_path)
    srv.routing = routing or router.Routing()
    srv.conn = store.connect(srv.data_dir / "queue.db")
    srv.stop_event = threading.Event()
    return srv


def main() -> None:
    token = os.environ.get("VOICE_INBOX_TOKEN", "")
    if not token:
        print("VOICE_INBOX_TOKEN not set; refusing to start", flush=True)
        raise SystemExit(1)
    data_dir = Path(os.environ.get(
        "VOICE_INBOX_DATA", Path.home() / ".local" / "share" / "voice-inbox"))
    model_path = Path(os.environ.get("VOICE_INBOX_WHISPER_MODEL",
                                     data_dir / "models" / "ggml-small.en.bin"))
    port = int(os.environ.get("VOICE_INBOX_PORT", "8790"))
    host = os.environ.get("VOICE_INBOX_HOST", "127.0.0.1")
    ring_token = os.environ.get("VOICE_INBOX_RING_TOKEN", "")

    handlers = load_all()
    try:
        routing = router.load()
    except router.ConfigError as exc:
        # A broken routes file is a typo someone can fix in seconds, and
        # starting anyway would route their speech nowhere. Fail loudly.
        print(f"[voice-inbox] bad routes config: {exc}", flush=True)
        raise SystemExit(2)

    srv = build_server(port, token, data_dir, model_path, routing,
                       ring_token=ring_token, host=host)
    threading.Thread(target=worker_loop, args=(srv,), daemon=True).start()
    print(f"[voice-inbox] listening on {host}:{port}\n"
          f"  data     {data_dir}\n"
          f"  routes   {len(routing.routes)} "
          f"({'fallback: ' + routing.fallback.handler if routing.fallback else 'no fallback'})\n"
          f"  handlers {', '.join(sorted(handlers)) or '(none)'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
