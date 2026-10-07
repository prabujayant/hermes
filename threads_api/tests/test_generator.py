import json

import pytest

from threads_api.config import Config
from threads_api.generator import (
    PILLARS,
    choose_template,
    hook_signature,
    load_state,
    load_templates,
    mark_used,
    next_thread,
    validate_thread,
)

BANK_DIR = Config.ROOT / "queue" / "bank"


def good_thread(n=5):
    posts = [f"HOOK LINE HERE {1}/{n}"]
    for i in range(2, n):
        posts.append(f"Value point number {i}. {i}/{n}")
    posts.append(f"Follow @owlsky17 so you don't miss the next one. {n}/{n}")
    return posts


def test_bank_loads_and_all_templates_valid():
    templates = load_templates(BANK_DIR)
    assert len(templates) >= 15, "bank should seed 15 threads"
    for template in templates:
        assert template["pillar"] in PILLARS
        assert validate_thread(template["posts"]) == []


def test_bank_covers_every_pillar():
    pillars = {t["pillar"] for t in load_templates(BANK_DIR)}
    assert pillars == set(PILLARS)


def test_validation_catches_style_violations():
    assert validate_thread(["x"]) != []  # wrong count, no counter/CTA
    bad_caps = good_thread()
    bad_caps[0] = f"lowercase hook 1/5"
    assert any("CAPS" in p for p in validate_thread(bad_caps))
    bad_cta = good_thread()
    bad_cta[-1] = "bye 5/5"
    assert any("CTA" in p for p in validate_thread(bad_cta))
    bad_len = good_thread()
    bad_len[2] = "x" * 501 + " 3/5"
    assert any("chars" in p for p in validate_thread(bad_len))
    bad_counter = good_thread()
    bad_counter[1] = "no counter here"
    assert any("counter" in p for p in validate_thread(bad_counter))


def test_hook_signature_stable():
    assert hook_signature("IF YOU WANT X STOP Y MEMORIZE THESE 5 THINGS: 1/5") == (
        "if you want x stop y memorize these"
    )


def test_rotation_cycles_pillars_without_repeating_hooks(tmp_path):
    bank = tmp_path / "bank"
    bank.mkdir()
    for pillar in PILLARS:
        (bank / f"{pillar}.json").write_text(
            json.dumps(
                [
                    {"pillar": pillar, "posts": good_thread()},
                    {
                        "pillar": pillar,
                        "posts": [
                            f"SECOND HOOK FOR {pillar.upper()} HERE 1/5"
                        ]
                        + good_thread()[1:],
                    },
                ]
            ),
            encoding="utf-8",
        )
    state_file = tmp_path / "state.json"
    seen_pillars = []
    for _ in range(len(PILLARS)):
        pillar, posts = next_thread(
            bank_dir=bank, state_path=state_file
        )
        seen_pillars.append(pillar)
        mark_used(pillar, posts, state_path=state_file)
    assert seen_pillars == list(PILLARS)


def test_dedup_skips_recent_hooks(tmp_path):
    bank = tmp_path / "bank"
    bank.mkdir()
    hook = good_thread()
    (bank / "leetcode.json").write_text(
        json.dumps([{"pillar": "leetcode", "posts": hook}]),
        encoding="utf-8",
    )
    state_file = tmp_path / "state.json"
    mark_used("leetcode", hook, state_path=state_file)
    state = load_state(state_file)
    with pytest.raises(RuntimeError, match="fresh templates"):
        choose_template(load_templates(bank), state)


def test_empty_bank_raises():
    with pytest.raises(RuntimeError, match="empty"):
        choose_template([], {})
