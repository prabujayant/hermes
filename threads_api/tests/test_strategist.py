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
    raw = "HOOK 1/3\n---\nValue 2/3\n---\nFollow @owlsky17 3/3\nHYPOTHESIS: hooks win"
    posts, hyp = parse_decision(raw)
    assert posts == ["HOOK 1/3", "Value 2/3", "Follow @owlsky17 3/3"]
    assert hyp == "hooks win"


def test_parse_decision_rejects_garbage():
    import pytest

    with pytest.raises(ValueError, match="2-7"):
        parse_decision("just one lonely post with no structure here")


def test_soft_validate_allows_style_experiments():
    posts = [
        "a lowercase debate hook, no caps at all 1/3",
        "value with opinion 2/3",
        "comment YOUR take below 3/3",
    ]
    assert soft_validate(posts) == []


def test_soft_validate_enforces_hard_constraints():
    problems = soft_validate(["only one post here 1/1"])
    assert any("CTA" in p for p in problems)
    problems = soft_validate(["x" * 501 + " 1/2", "Follow @owlsky17 2/2"])
    assert any("chars" in p for p in problems)
    problems = soft_validate(["hook 1/2", "no cta here 2/2"])
    assert any("CTA" in p for p in problems)
    problems = soft_validate(["hook", "Follow @owlsky17 2/2"])
    assert any("counter" in p for p in problems)


def test_pivot_constant_matches_threshold():
    assert PIVOT_STREAK == 2


def test_repair_counters_appends_only_missing():
    from threads_api.strategist import repair_counters

    posts = ["hook with 1/3", "plain value post", "cta with 3/3"]
    fixed = repair_counters(posts)
    assert fixed[0] == "hook with 1/3"
    assert fixed[1].endswith(" 2/3")
    assert fixed[2] == "cta with 3/3"


def test_repair_counters_salvages_failed_run_shape():
    from threads_api.strategist import repair_counters, soft_validate

    posts = [f"value point number {i} here" for i in range(1, 7)]
    posts.append("Follow @owlsky17 for more")
    assert soft_validate(repair_counters(posts)) == []


def test_repair_thread_appends_missing_cta():
    from threads_api.strategist import repair_thread, soft_validate

    posts = [f"value point number {i} here" for i in range(1, 5)]
    assert soft_validate(repair_thread(posts)) == []
    assert "Follow @owlsky17" in repair_thread(posts)[-1]


def test_repair_thread_keeps_comment_cta():
    from threads_api.strategist import repair_thread

    posts = ["hook here", "value here", "comment YOUR take here"]
    fixed = repair_thread(posts)
    assert "Follow @owlsky17" not in fixed[-1]
    assert fixed[-1].endswith("3/3")
