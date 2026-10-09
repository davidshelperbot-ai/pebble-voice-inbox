"""The registry and the two generic handlers.

The generic handlers matter most: they are how someone adds an integration
without writing any Python, so their contract needs pinning.
"""
import json
import sys

import pytest

from voice_inbox import handlers
from voice_inbox.handlers import Command, Result


@pytest.fixture(autouse=True)
def clean_registry():
    handlers.load_all(plugin_dir=None)   # built-ins must be present
    saved = handlers.available()
    yield
    handlers._REGISTRY.clear()
    handlers._REGISTRY.update(saved)


def test_a_decorated_function_is_registered():
    @handlers.handler("probe")
    def probe(cmd):
        return Result.done("ok")

    assert handlers.get("probe") is probe


def test_registering_a_duplicate_name_is_refused():
    handlers.register("dupe", lambda cmd: Result.done())
    with pytest.raises(ValueError):
        handlers.register("dupe", lambda cmd: Result.done())


def test_an_unknown_handler_names_the_alternatives():
    handlers.register("one", lambda cmd: Result.done())
    with pytest.raises(KeyError) as e:
        handlers.get("nope")
    assert "one" in str(e.value)


def test_a_py_file_dropped_in_the_plugin_dir_is_loaded(tmp_path):
    """The no-packaging path: one file in a folder adds a route target."""
    (tmp_path / "mine.py").write_text(
        "from voice_inbox.handlers import handler, Result\n"
        "@handler('mine')\n"
        "def mine(cmd):\n"
        "    return Result.done('from a plugin')\n")
    handlers.load_all(plugin_dir=tmp_path)
    assert handlers.get("mine")(Command(text="x")).detail == "from a plugin"


def test_a_broken_plugin_does_not_take_the_service_down(tmp_path, capsys):
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "fine.py").write_text(
        "from voice_inbox.handlers import handler, Result\n"
        "@handler('fine')\n"
        "def fine(cmd):\n    return Result.done()\n")
    handlers.load_all(plugin_dir=tmp_path)
    assert handlers.get("fine"), "a sibling plugin's syntax error must not matter"
    assert "broken.py" in capsys.readouterr().out


def test_underscore_files_are_skipped(tmp_path):
    (tmp_path / "_private.py").write_text("raise RuntimeError('should not run')\n")
    handlers.load_all(plugin_dir=tmp_path)  # must not raise


# --- the `command` handler --------------------------------------------------

def cmd_for(text="hello world", **config):
    return Command(text=text, source="ring", config=config)


def test_command_handler_passes_the_transcript_on_stdin(tmp_path):
    out = tmp_path / "got.txt"
    r = handlers.get("command")(cmd_for(
        command=[sys.executable, "-c",
                 f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"]))
    assert r.ok
    assert out.read_text() == "hello world"


def test_dictated_text_is_never_spliced_into_a_command_line(tmp_path):
    """A transcript containing a semicolon must not become a second command."""
    out = tmp_path / "got.txt"
    nasty = "it's fine; echo PWNED"
    r = handlers.get("command")(cmd_for(
        nasty,
        command=[sys.executable, "-c",
                 f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"]))
    assert r.ok
    assert out.read_text() == nasty


def test_named_groups_arrive_as_environment_variables(tmp_path):
    out = tmp_path / "got.txt"
    c = Command(text="t", groups={"who": "alex"},
                config={"command": [
                    sys.executable, "-c",
                    f"import os;open({str(out)!r},'w').write(os.environ['VOICE_GROUP_WHO'])"]})
    assert handlers.get("command")(c).ok
    assert out.read_text() == "alex"


def test_a_failing_command_is_reported_with_its_stderr():
    r = handlers.get("command")(cmd_for(
        command=[sys.executable, "-c", "import sys;sys.stderr.write('nope');sys.exit(3)"]))
    assert not r.ok and "nope" in r.detail


def test_a_missing_command_is_not_retried_forever():
    r = handlers.get("command")(cmd_for(command=["definitely-not-a-real-binary-xyz"]))
    assert not r.ok and r.final


def test_a_route_with_no_command_says_so():
    assert "no `command`" in handlers.get("command")(cmd_for()).detail


# --- the `webhook` handler --------------------------------------------------

def test_webhook_posts_the_transcript_as_json(monkeypatch):
    seen = {}

    class FakeResp:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = handlers.get("webhook")(cmd_for(
        "buy milk", url="https://example.com/h",
        headers={"Authorization": "Bearer tok"}))
    assert r.ok
    assert seen["url"] == "https://example.com/h"
    assert seen["body"]["text"] == "buy milk"
    assert seen["headers"]["authorization"] == "Bearer tok"


def test_webhook_without_a_url_says_so():
    assert "no `url`" in handlers.get("webhook")(cmd_for()).detail


def test_a_4xx_is_final_but_a_5xx_is_retried(monkeypatch):
    import urllib.error

    def raiser(code):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "no", {}, None)
        return fake

    monkeypatch.setattr("urllib.request.urlopen", raiser(400))
    assert handlers.get("webhook")(cmd_for(url="https://e.com")).final is True
    monkeypatch.setattr("urllib.request.urlopen", raiser(503))
    assert handlers.get("webhook")(cmd_for(url="https://e.com")).final is False
