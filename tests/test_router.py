"""Routing is configuration, so the config file is part of the contract."""
import pytest

from voice_inbox import router

CONFIG = r"""
[[route]]
name = "text someone"
match = '^(?:text|message)\s+(?P<who>\S+)\s+(?P<body>.+)$'
handler = "imessage"
contacts = { alex = "+15555550123" }

[[route]]
name = "note"
match = '^(?:note|remember)\s+(?P<body>.+)$'
handler = "webhook"
url = "https://example.com/notes"

[fallback]
handler = "log"
"""


def load(text, tmp_path):
    p = tmp_path / "routes.toml"
    p.write_text(text)
    return router.load(p)


def test_first_matching_route_wins(tmp_path):
    r = load(CONFIG, tmp_path)
    route, groups = r.resolve("text alex running late")
    assert route.handler == "imessage"
    assert groups == {"who": "alex", "body": "running late"}


def test_named_groups_reach_the_handler(tmp_path):
    r = load(CONFIG, tmp_path)
    _, groups = r.resolve("remember the bins go out tuesday")
    assert groups["body"] == "the bins go out tuesday"


def test_per_route_config_is_passed_through(tmp_path):
    r = load(CONFIG, tmp_path)
    route, _ = r.resolve("text alex hi")
    assert route.config["contacts"] == {"alex": "+15555550123"}
    assert "handler" not in route.config and "match" not in route.config


def test_matching_is_case_insensitive(tmp_path):
    r = load(CONFIG, tmp_path)
    route, _ = r.resolve("TEXT alex HI")
    assert route.handler == "imessage"


def test_unmatched_text_goes_to_the_fallback(tmp_path):
    r = load(CONFIG, tmp_path)
    route, groups = r.resolve("what even is this")
    assert route.handler == "log" and groups == {}


def test_without_a_fallback_unmatched_text_is_unresolved(tmp_path):
    """Returning None rather than guessing: sending speech to the wrong
    integration is worse than admitting nothing claimed it."""
    r = load("""
[[route]]
match = '^note (?P<body>.+)$'
handler = "log"
""", tmp_path)
    assert r.resolve("text alex hi") is None


def test_a_missing_config_is_not_an_error(tmp_path):
    """The service must still accept and queue dictation with no routes yet --
    holding speech beats dropping it."""
    r = router.load(tmp_path / "absent.toml")
    assert r.routes == () and r.fallback is None


def test_a_route_without_a_handler_is_rejected_by_name(tmp_path):
    with pytest.raises(router.ConfigError) as e:
        load('[[route]]\nname = "oops"\nmatch = "x"\n', tmp_path)
    assert "oops" in str(e.value)


def test_a_route_without_a_pattern_is_rejected(tmp_path):
    with pytest.raises(router.ConfigError) as e:
        load('[[route]]\nname = "oops"\nhandler = "log"\n', tmp_path)
    assert "fallback" in str(e.value), "should point at the right tool for a catch-all"


def test_an_invalid_regex_names_the_route(tmp_path):
    with pytest.raises(router.ConfigError) as e:
        load('[[route]]\nname = "bad"\nmatch = "([unclosed"\nhandler = "log"\n',
             tmp_path)
    assert "bad" in str(e.value)


def test_malformed_toml_is_reported_clearly(tmp_path):
    with pytest.raises(router.ConfigError) as e:
        load("[[route]\nbroken", tmp_path)
    assert "TOML" in str(e.value)
