import datetime

from threads_api.replier import (
    build_reply_prompt,
    collect_pending,
    parse_replies,
    validate_reply,
)


def _comment(cid, user, hours_ago, text="nice post"):
    born = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=hours_ago)
    return {"id": cid, "username": user, "text": text,
            "timestamp": born.strftime("%Y-%m-%dT%H:%M:%S+0000")}


class FakeClient:
    def __init__(self, threads):
        self.threads = threads  # {post_id: [comments]}

    def list_posts(self, limit=10):
        return {"data": [{"id": pid} for pid in list(self.threads)[:limit]]}

    def _get(self, endpoint, params):
        pid = endpoint.split("/")[0]
        return {"data": self.threads.get(pid, [])}


def test_collect_skips_own_answered_and_old(tmp_path, monkeypatch):
    import threads_api.replier as mod

    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "r.json")
    (tmp_path / "r.json").write_text('{"answered": ["c-old-done"]}',
                                     encoding="utf-8")
    client = FakeClient({"p1": [
        _comment("c-mine", "owlsky17", 2),
        _comment("c-done", "fan", 2),
        _comment("c-old-done", "fan", 2),
        _comment("c-stale", "fan", 72),
        _comment("c-good", "fan", 5, "how do indexes work?"),
    ]})
    # mark c-done answered via state
    import json
    (tmp_path / "r.json").write_text(
        json.dumps({"answered": ["c-done", "c-old-done"]}), encoding="utf-8")
    pending, _, scanned = collect_pending(client, "owlsky17")
    assert [c["id"] for c in pending] == ["c-good"]
    assert scanned == 1


def test_parse_replies_maps_ids():
    raw = "[c1]\nGreat question, yes!\n---\n[c2]\nExactly, indexes first."
    got = parse_replies(raw, ["c1", "c2", "c3"])
    assert got == {"c1": "Great question, yes!",
                   "c2": "Exactly, indexes first."}


def test_parse_replies_rejects_garbage():
    import pytest

    with pytest.raises(ValueError, match="no usable replies"):
        parse_replies("hello with no ids", ["c1"])


def test_validate_reply_rules():
    assert validate_reply("") != []
    assert validate_reply("x" * 281) != []
    assert validate_reply("nice — dash") != []
    assert validate_reply("thanks! Follow @owlsky17 here") != []
    assert validate_reply("Good catch, the index covers it. "
                          "What query hit you?") == []


def test_prompt_lists_every_comment():
    prompt = build_reply_prompt([
        {"id": "c1", "author": "a", "text": "q1"},
        {"id": "c2", "author": "b", "text": "q2"},
    ])
    assert "[c1]" in prompt and "[c2]" in prompt
    assert "280" in prompt
