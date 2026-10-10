"""Template-bank content engine for the hourly auto-poster.

Every thread produced here must satisfy queue/STYLE.md:
caps hook, sentence-case value replies, follow-CTA last post,
each post <= 500 chars. No post counters.
"""

import json
import re
from pathlib import Path

from .config import Config

PILLARS = ("leetcode", "backend", "sql", "interviews", "career",
            "ai-coding", "system-design")
BANK_DIR = Config.ROOT / "queue" / "bank"
STATE_PATH = Config.ROOT / "queue" / "state.json"
POSTED_LOG = Config.ROOT / "queue" / "posted.jsonl"
RECENT_HOOKS = 30
FOLLOW_CTA = "Follow @owlsky17"


def hook_signature(text):
    """First 8 words, lowercased — used for dedup."""
    return " ".join(text.lower().split()[:8])


def load_templates(bank_dir=BANK_DIR):
    """Load every thread template from queue/bank/*.json."""
    templates = []
    if not Path(bank_dir).exists():
        return templates
    for path in sorted(Path(bank_dir).glob("*.json")):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("posts"):
                templates.append(
                    {
                        "pillar": entry.get("pillar", path.stem),
                        "posts": list(entry["posts"]),
                        "source": path.name,
                    }
                )
    return templates


def load_state(state_path=STATE_PATH):
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        state = {}
    state.setdefault("last_pillar", None)
    state.setdefault("recent_hooks", [])
    state.setdefault("last_run", None)
    return state


def save_state(state, state_path=STATE_PATH):
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def choose_template(templates, state):
    """Round-robin pillars, skipping recently used hooks.

    Raises RuntimeError if the bank is empty or everything was used
    recently (caller should widen the bank, not repeat a hook).
    """
    if not templates:
        raise RuntimeError(
            "Template bank is empty — add threads to queue/bank/*.json"
        )
    recent = set(state.get("recent_hooks", [])[-RECENT_HOOKS:])
    start = 0
    if state.get("last_pillar") in PILLARS:
        start = (PILLARS.index(state["last_pillar"]) + 1) % len(PILLARS)

    for offset in range(len(PILLARS)):
        pillar = PILLARS[(start + offset) % len(PILLARS)]
        for template in templates:
            if template["pillar"] != pillar:
                continue
            if hook_signature(template["posts"][0]) not in recent:
                return template

    raise RuntimeError(
        f"All hooks used in the last {RECENT_HOOKS} posts — "
        "add fresh templates to queue/bank/ before the next run"
    )


def validate_thread(posts):
    """Enforce STYLE.md. Returns list of problems (empty = valid)."""
    import re

    problems = []
    n = len(posts)
    if not 5 <= n <= 7:
        problems.append(f"thread has {n} posts, need 5-7")
    for i, post in enumerate(posts, start=1):
        if len(post) > 500:
            problems.append(f"post {i}: {len(post)} chars, max 500")
        if re.search(r"\d+/\d+", post):
            problems.append(f"post {i}: counters like 1/5 are not allowed")
    if posts:
        letters = [c for c in posts[0] if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) < 0.7:
            problems.append("hook is not ALL CAPS")
    if posts and FOLLOW_CTA not in posts[-1]:
        problems.append(f"last post missing {FOLLOW_CTA!r} CTA")
    return problems


def next_thread(bank_dir=BANK_DIR, state_path=STATE_PATH):
    """Pick the next thread and return (pillar, posts).

    State is NOT saved here — the worker saves it after a
    successful publish so failed runs never burn a template.
    """
    templates = load_templates(bank_dir)
    state = load_state(state_path)
    template = choose_template(templates, state)
    problems = validate_thread(template["posts"])
    if problems:
        raise RuntimeError(
            f"Template from {template['source']} violates STYLE.md: "
            + "; ".join(problems)
        )
    return template["pillar"], list(template["posts"])


def mark_used(pillar, posts, state_path=STATE_PATH, run_at=None):
    """Record a published thread in state.json. Call after publish."""
    state = load_state(state_path)
    state["last_pillar"] = pillar
    recent = state.get("recent_hooks", [])[-RECENT_HOOKS + 1:]
    recent.append(hook_signature(posts[0]))
    state["recent_hooks"] = recent
    if run_at:
        state["last_run"] = run_at
    save_state(state, state_path)
