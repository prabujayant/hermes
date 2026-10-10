import datetime

from threads_api.strategist import (
    PIVOT_STREAK,
    build_prompt,
    evaluate,
    parse_decision,
    soft_validate,
)


def _post(text, views, hours_ago, eng=5):
    born = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=hours_ago
    )
    return {
        "text": text,
        "timestamp": born.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "metrics": {"views": views, "likes": eng, "replies": 0,
                    "reposts": 0, "quotes": 0, "shares": 0},
    }


def test_evaluate_judges_only_old_posts():
    posts = [_post("fresh viral", 1000, 1), _post("old flop", 5, 5)]
    score = evaluate(posts)
    assert len(score["judged"]) == 1
    assert score["judged"][0]["text"] == "old flop"


def test_evaluate_streak_and_pivot():
    posts = [
        _post("winner", 500, 7),
        _post("flop one", 5, 6),
        _post("flop two", 5, 5),
    ]
    score = evaluate(posts)
    assert score["median"] > 0
    assert score["streak"] == 2
    assert score["pivot"] is True


def test_evaluate_streak_resets_on_winner():
    posts = [
        _post("flop", 5, 8),
        _post("winner", 500, 5),
    ]
    score = evaluate(posts)
    assert score["streak"] == 0
    assert score["pivot"] is False


def test_build_prompt_contains_pivot_orders():
    posts = [_post("flop one", 5, 6), _post("flop two", 5, 7)]
    score = evaluate(posts)
    prompt = build_prompt(score, {"hypothesis": "old idea"})
    assert "PIVOT MODE" in prompt
    assert "UNDER" in prompt


def test_build_prompt_no_pivot_when_winning():
    posts = [_post("winner", 500, 5)]
    score = evaluate(posts)
    prompt = build_prompt(score, {"hypothesis": "x"})
    assert "PIVOT MODE" not in prompt


def test_parse_decision_splits_posts_and_hypothesis():
    raw = "HOOK\n---\nValue\n---\nFollow @owlsky17\nHYPOTHESIS: hooks win"
    posts, hyp = parse_decision(raw)
    assert posts == ["HOOK", "Value", "Follow @owlsky17"]
    assert hyp == "hooks win"


def test_parse_decision_rejects_garbage():
    import pytest

    with pytest.raises(ValueError, match="2-7"):
        parse_decision("just one lonely post with no structure here")


def test_soft_validate_allows_style_experiments():
    posts = [
        "a lowercase debate hook, no caps at all",
        "value with opinion",
        "Follow @owlsky17 for more",
    ]
    assert soft_validate(posts) == []


def test_soft_validate_enforces_hard_constraints():
    problems = soft_validate(["only one post here"])
    assert any("CTA" in p for p in problems)
    problems = soft_validate(["x" * 501, "Follow @owlsky17"])
    assert any("chars" in p for p in problems)
    problems = soft_validate(["hook", "no cta here"])
    assert any("CTA" in p for p in problems)
    problems = soft_validate(["hook 1/2", "Follow @owlsky17"])
    assert any("counter" in p for p in problems)


def test_finalize_thread_end_to_end():
    from threads_api.strategist import finalize_thread

    raw = ("HOOK HERE — WITH DASH 1/5\n---\nvalue here\n---\n"
           "comment below\nHYPOTHESIS: x")
    posts, hyp = finalize_thread(raw)
    assert hyp == "x"
    assert posts[0] == "HOOK HERE, WITH DASH"
    assert "Follow @owlsky17" in posts[-1]
    assert not any("1/5" in p for p in posts)


def test_pivot_constant_matches_threshold():
    assert PIVOT_STREAK == 2


def test_build_prompt_bans_counters():
    posts = [_post("winner", 500, 5)]
    prompt = build_prompt(evaluate(posts), {"hypothesis": "x"})
    assert "do NOT number the posts" in prompt
    assert "do NOT write counters" in prompt


def test_repair_thread_strips_counters():
    from threads_api.strategist import repair_thread, soft_validate

    posts = ["hook here 1/3", "plain value post 2/3", "cta here 3/3"]
    fixed = repair_thread(posts)
    assert fixed == ["hook here", "plain value post",
                     "cta here\n\nFollow @owlsky17 so you don't "
                     "miss the next one."]
    assert soft_validate(fixed) == []


def test_repair_thread_salvages_failed_run_shape():
    from threads_api.strategist import repair_thread, soft_validate

    posts = [f"value point number {i} here" for i in range(1, 7)]
    posts.append("Follow @owlsky17 for more")
    assert soft_validate(repair_thread(posts)) == []


def test_repair_thread_appends_missing_cta():
    from threads_api.strategist import repair_thread, soft_validate

    posts = [f"value point number {i} here" for i in range(1, 5)]
    assert soft_validate(repair_thread(posts)) == []
    assert "Follow @owlsky17" in repair_thread(posts)[-1]


def test_repair_thread_appends_follow_over_comment_cta():
    from threads_api.strategist import repair_thread

    posts = ["hook here", "value here", "comment YOUR take here"]
    fixed = repair_thread(posts)
    assert "Follow @owlsky17" in fixed[-1]
    assert "1/5" not in fixed[-1] and "3/3" not in fixed[-1]


def test_ascii_clean_removes_fancy_dashes():
    from threads_api.strategist import ascii_clean

    assert ascii_clean("a — b") == "a, b"
    assert ascii_clean("a–b") == "a-b"
    assert ascii_clean("plain - text") == "plain - text"


def test_repair_thread_normalises_dashes():
    from threads_api.strategist import repair_thread, soft_validate

    posts = ["hook here — with dash", "value–here",
             "Follow @owlsky17 for more"]
    fixed = repair_thread(posts)
    assert soft_validate(fixed) == []
    assert all("—" not in p and "–" not in p for p in fixed)


def test_soft_validate_rejects_dashes_and_comment_only_cta():
    from threads_api.strategist import soft_validate

    assert any("non-ASCII" in p
               for p in soft_validate(["hook — here 1/2",
                                        "Follow @owlsky17 2/2"]))
    assert any("follow CTA" in p
               for p in soft_validate(["hook 1/2", "comment below 2/2"]))
