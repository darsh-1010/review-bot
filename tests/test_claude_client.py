import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src/reviewer"))
import claude_client as cc  # noqa: E402


def fb(vote, msg="x", cat="bug", sev="high"):
    return {"vote": vote, "message": msg, "category": cat, "severity": sev}


def test_no_feedback_no_hints():
    assert cc.hints_text([]) == ""


def test_hints_split_up_and_down_and_flatten_newlines():
    t = cc.hints_text([fb("up", "null\ncheck"), fb("down", "rename var", "readability", "low")])
    assert "USEFUL" in t and "bug/high: null check" in t
    assert "NOT useful" in t and "readability/low: rename var" in t


def test_estimate_grows_with_diff_and_model():
    small, big = cc.estimate_micro("claude-haiku-5-5", "a" * 400), cc.estimate_micro("claude-haiku-5-5", "a" * 400_000)
    assert big > small
    assert cc.estimate_micro("claude-sonnet-5-5", "a" * 400) > small


def test_cost_cap_blocks_a_huge_chunk():
    cap = 100_000  # $0.10; a 240k-token Sonnet chunk is worst-case far above that
    assert cc.estimate_micro("claude-sonnet-5-5", "a" * 960_000) > cap
    assert cc.estimate_micro("claude-haiku-5-5", "a" * 4_000) < cap


def test_review_chunk_skips_thinking_block():
    from types import SimpleNamespace as N
    resp = N(content=[N(type="thinking"), N(type="text", text='{"summary": "s", "issues": []}')], usage="u")
    client = N(messages=N(create=lambda **kw: resp))
    assert cc.review_chunk(client, "claude-haiku-5-5", "d") == ({"summary": "s", "issues": []}, "u")
