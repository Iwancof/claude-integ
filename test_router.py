#!/usr/bin/env python3
"""Run: python test_router.py"""

import importlib.util
import sys

spec = importlib.util.spec_from_file_location("cr", "claude_router.py")
cr = importlib.util.module_from_spec(spec)
sys.modules["cr"] = cr  # dataclass needs the module registered
spec.loader.exec_module(cr)


def test_wants_server_tool():
    f = cr._wants_server_tool
    assert f({"tools": [{"type": "web_search_20260209", "name": "web_search"}]}) is True
    assert f({"tools": [{"type": "web_search_20250305", "name": "web_search"}]}) is True
    # The session's own client-side WebSearch tool keeps model-based routing.
    assert f({"tools": [{"name": "WebSearch", "input_schema": {}}]}) is False
    # The marker also occurs in conversation text; that must not reroute a
    # whole vendor session to Anthropic.
    assert f({"tools": [{"name": "Bash"}],
              "messages": [{"role": "user", "content": "grep web_search_20260209"}]}) is False
    for bad in (None, {}, {"tools": "nope"}, {"tools": [None, 3, {"type": None}]}):
        assert f(bad) is False


def test_pick_longest_prefix_wins():
    b = [cr.Backend("kimi", "https", "h", "", "t", ["kimi-", "k3"]),
         cr.Backend("anthropic", "https", "h", "", None, [])]
    r = cr.Router(b, b[1])
    assert r.pick("k3").name == "kimi"
    assert r.pick("claude-opus-5").name == "anthropic"
    assert r.pick(None).name == "anthropic"


if __name__ == "__main__":
    test_wants_server_tool()
    test_pick_longest_prefix_wins()
    print("ok")
