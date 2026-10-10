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
    assert score["median_vph"] > 0
    assert score["streak"] == 2
    assert score["ready"] is True
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
    posts = [_post("flop one", 5, 8), _post("flop two", 5, 7),
             _post("flop three", 5, 6)]
    score = evaluate(posts)
    assert score["pivot"] is True
    prompt = build_prompt(score, {"hypothesis": "old idea"})
    assert "PIVOT MODE" in prompt
    assert "UNDER" in prompt
    assert "AT MOST ONE factor" in prompt
    assert "P1" in prompt


def test_prompt_includes_niche_leaders_brief(tmp_path):
    from threads_api import strategist as st

    brief_file = tmp_path / "niche.md"
    brief_file.write_text("LEADER FORMAT XYZ", encoding="utf-8")
    real_path = st.NICHE_PATH
    st.NICHE_PATH = brief_file
    try:
        posts = [_post("winner", 500, 5)]
        prompt = build_prompt(evaluate(posts), {"hypothesis": "x"})
        assert "LEADER FORMAT XYZ" in prompt
        assert "imitate as-is" in prompt.lower()
    finally:
        st.NICHE_PATH = real_path


def test_niche_brief_file_exists_and_names_leaders():
    from pathlib import Path

    from threads_api.config import Config

    text = (Config.ROOT / "queue" / "niche_leaders.md").read_text(
        encoding="utf-8")
    for name in ("Pocock", "roby", "Sahil", "follow CTA"):
        assert name in text


def test_niche_posts_sorted_views_desc_nulls_last(tmp_path):
    import json

    from threads_api.strategist import load_niche_posts

    path = tmp_path / "niche_posts.json"
    path.write_text(json.dumps({"posts": [
        {"creator": "noname", "hook": "h", "views": None},
        {"creator": "mid", "hook": "h", "views": 100},
        {"creator": "top", "hook": "h", "views": 5000},
        {"creator": "low", "hook": "h", "views": 50},
    ]}), encoding="utf-8")
    got = load_niche_posts(path=path)
    assert [e["creator"] for e in got] == ["top", "mid", "low", "noname"]


def test_niche_posts_missing_file_returns_empty(tmp_path):
    from threads_api.strategist import load_niche_posts

    assert load_niche_posts(path=tmp_path / "nope.json") == []


def test_niche_scan_section_shows_freshness_and_scope(tmp_path):
    import json

    from threads_api import strategist as st

    path = tmp_path / "scan.json"
    path.write_text(json.dumps({
        "fetched": "2026-10-10T00:00:00+00:00",
        "public": True,
        "items": [{"author": "prime", "text": "hot take here",
                   "timestamp": "", "permalink": "", "via": "q"}],
    }), encoding="utf-8")
    fetched, public, items = st.load_niche_scan(path=path)
    assert public is True
    assert items[0]["author"] == "prime"

    monkeypatch_holder = {"orig": st.load_niche_scan}
    st.load_niche_scan = lambda: (
        "2026-10-10T00:00:00+00:00", True, items)
    try:
        posts = [_post("winner", 500, 5)]
        prompt = build_prompt(evaluate(posts), {"hypothesis": "x"})
        assert "LIVE NICHE SCAN" in prompt
        assert "@prime" in prompt
        assert "hot take here" in prompt
    finally:
        st.load_niche_scan = monkeypatch_holder["orig"]


def test_prompt_leads_with_top_earner():
    posts = [_post("winner", 500, 5)]
    prompt = build_prompt(evaluate(posts), {"hypothesis": "x"})
    assert "NICHE PROOF" in prompt
    assert "2,200,000 views" in prompt
    proof = prompt[prompt.index("NICHE PROOF"):]
    assert proof.index("ThePrimeagen") < proof.index("codingwithroby")


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


def test_track_progress_computes_deltas():
    from threads_api.strategist import track_progress

    state = {"last_followers": 250, "last_views": 2000}
    progress = track_progress(
        state, {"followers": 258, "views": 2114, "likes": 47})
    assert progress == {"followers": 258, "followers_delta": 8,
                        "views_total": 2114, "views_delta": 114}
    assert state["last_followers"] == 258
    assert state["last_views"] == 2114


def test_track_progress_first_run_has_no_deltas():
    from threads_api.strategist import track_progress

    progress = track_progress(
        {}, {"followers": 258, "views": 2114, "likes": 0})
    assert progress["followers_delta"] is None
    assert progress["views_delta"] is None


def test_prompt_carries_mission_and_progress():
    posts = [_post("winner", 500, 5)]
    prompt = build_prompt(
        evaluate(posts), {"hypothesis": "x"},
        {"followers": 258, "followers_delta": 8,
         "views_total": 2114, "views_delta": 114})
    assert "1,000,000,000" in prompt
    assert "1,000,000" in prompt
    assert "258 followers" in prompt
    assert "NEVER fake numbers" in prompt


def test_sprint_block_shows_pace_and_deadline():
    import datetime

    from threads_api.strategist import SPRINT_END, sprint_status

    assert SPRINT_END == datetime.date(2026, 11, 10)
    pace = sprint_status(today=datetime.date(2026, 10, 10),
                         followers=258, views=4000)
    assert pace["days_left"] == 31
    assert pace["followers_per_day_needed"] > 30000
    assert pace["views_per_day_needed"] > 30000000
    posts = [_post("winner", 500, 5)]
    prompt = build_prompt(
        evaluate(posts), {"hypothesis": "x"},
        {"followers": 258, "followers_delta": 0,
         "views_total": 4000, "views_delta": 0})
    assert "SPRINT" in prompt
    assert "Swing for fences" in prompt


def test_sprint_past_deadline_never_negative():
    import datetime

    from threads_api.strategist import sprint_status

    pace = sprint_status(today=datetime.date(2026, 12, 1),
                         followers=10, views=10)
    assert pace["days_left"] == 0
    assert pace["followers_per_day_needed"] == 0


def test_prompt_without_progress_still_has_mission():
    posts = [_post("winner", 500, 5)]
    prompt = build_prompt(evaluate(posts), {"hypothesis": "x"})
    assert "MISSION" in prompt


def test_finalize_thread_end_to_end():
    from threads_api.strategist import finalize_thread

    raw = ("HOOK HERE — WITH DASH 1/5\n---\nvalue here\n---\n"
           "comment below\nHYPOTHESIS: x")
    posts, hyp = finalize_thread(raw)
    assert hyp == "x"
    assert posts[0] == "HOOK HERE, WITH DASH"
    assert "Follow @owlsky17" in posts[-1]
    assert not any("1/5" in p for p in posts)


def test_evaluate_excludes_posts_without_view_data():
    born = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
    no_data = {
        "text": "mystery post",
        "timestamp": born.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "metrics": {},
    }
    posts = [no_data, _post("real flop", 5, 6), _post("real ok", 200, 7)]
    score = evaluate(posts)
    texts = [item["text"] for item in score["judged"]]
    assert "mystery post" not in texts
    assert len(score["judged"]) == 2


def test_evaluate_compares_velocity_not_raw_totals():
    # 100 views in 20h (5/h) vs 30 views in 4h (7.5/h): raw totals would
    # call the second a flop; velocity judges fairly.
    posts = [
        _post("slow burner", 100, 20),
        _post("fast starter", 30, 4),
        _post("anchor", 60, 10),
    ]
    score = evaluate(posts)
    by_text = {item["text"]: item for item in score["judged"]}
    assert by_text["slow burner"]["vph"] == 5.0
    assert by_text["fast starter"]["vph"] == 7.5
    # Neither is far below the median velocity: no false flop.
    assert by_text["fast starter"]["under"] is False


def test_no_pivot_without_minimum_sample():
    posts = [_post("flop one", 5, 6), _post("flop two", 5, 5)]
    score = evaluate(posts)
    assert score["sample_size"] == 2
    assert score["streak"] == 2
    assert score["ready"] is False
    assert score["pivot"] is False


def test_judged_items_carry_full_metrics_and_ids():
    posts = [_post("winner", 500, 7), _post("flop", 5, 6),
             _post("mid", 100, 5)]
    score = evaluate(posts)
    ids = [item["id"] for item in score["judged"]]
    assert ids == ["P1", "P2", "P3"]
    first = score["judged"][0]
    for key in ("views", "vph", "likes", "replies", "reposts",
                "rate_pct", "age_h", "text", "under"):
        assert key in first


def test_record_run_saves_texts_model_hypothesis(tmp_path):
    from threads_api.strategist import load_strategy, record_run

    state_file = tmp_path / "strategy.json"
    state = load_strategy(state_file)
    state["hypothesis"] = "test idea"
    record_run(state, "smart", ["hook", "Follow @owlsky17"], ["id1"],
               "2026-01-01T00:00:00+00:00", True, model="test-model",
               path=state_file)
    saved = load_strategy(state_file)
    entry = saved["history"][-1]
    assert entry["texts"] == ["hook", "Follow @owlsky17"]
    assert entry["model"] == "test-model"
    assert entry["hypothesis"] == "test idea"
    assert entry["pivoted"] is True
    assert saved["pivots"] == 1


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


def test_brain_chain_paid_primary_free_fallback():
    from threads_api.strategist import BRAIN_MODELS, SMART_MODELS

    assert BRAIN_MODELS[0] == "openai/gpt-6-luna-pro"
    assert SMART_MODELS[0] == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_decide_direction_parses_four_lines():
    from threads_api.strategist import decide_direction

    def fake_chat(messages, max_tokens=400, temperature=0.7, model=None):
        return ("TOPIC: backend caching\n"
                "HOOK: debate hot take on cache stampedes\n"
                "CHANGE: switch from listicles to debate\n"
                "HYPOTHESIS: debates earn replies")

    posts = [_post("winner", 500, 5)]
    direction = decide_direction(
        fake_chat, ["m"], evaluate(posts),
        {"hypothesis": "old"})
    assert direction == {
        "topic": "backend caching",
        "hook": "debate hot take on cache stampedes",
        "change": "switch from listicles to debate",
        "hypothesis": "debates earn replies",
    }


def test_decide_direction_rejects_garbage():
    import pytest

    from threads_api.strategist import decide_direction

    def fake_chat(messages, max_tokens=400, temperature=0.7, model=None):
        return "just some rambling with no fields at all"

    posts = [_post("winner", 500, 5)]
    with pytest.raises(ValueError, match="TOPIC"):
        decide_direction(fake_chat, ["m"], evaluate(posts),
                         {"hypothesis": "old"})
