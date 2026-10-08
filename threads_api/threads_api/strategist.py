"""Autonomous strategist: measure views, decide the next thread, pivot.

Closed loop (runs hourly, free models only — never paid in auto mode):
  1. MEASURE: fetch recent posts + insights, score posts old enough to
     judge (>= 3h), compare against the rolling median.
  2. DECIDE: one LLM call gets the scorecard + current hypothesis and
     returns the next thread plus an updated hypothesis.
  3. PIVOT: 2+ consecutive underperformers -> the prompt orders a
     strategy change (new angle/format/topic), recorded in state.

The model has freedom to experiment: only API-hard constraints are
enforced (<=500 chars, 2-7 posts, counters, a CTA). Hook style, topics
and formats are its playground — views are the judge.
"""

import datetime
import json
from pathlib import Path

from .config import Config
from .generator import hook_signature
from .insights import engagement_of, views_of

STRATEGY_PATH = Config.ROOT / "queue" / "strategy.json"

# Free-only chain for the autonomous loop (never spend in auto mode).
SMART_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
]

JUDGE_AFTER_HOURS = 3
UNDERPERFORM_RATIO = 0.5
PIVOT_STREAK = 2
LOOKBACK_POSTS = 20

NICHE_PLAYBOOK = """Proven viral moves in the coding niche on Threads:
- Curiosity-gap opener (open loop, payoff inside the thread).
- Caps-listicle hook: IF YOU WANT X, STOP Y. MEMORIZE THESE N Z.
- Debate hot take ending in a challenge (highest reply rates).
- Contrarian lessons: 'most X advice is outdated, here is what changed'.
- Cheatsheet/stack thread ending in a comment CTA (comment X for Y).
- Algorithm facts: replies (esp. replies-to-replies in the first hours),
  profile clicks and view-time drive reach. One topic tag per post."""


def load_strategy(path=STRATEGY_PATH):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        state = {}
    state.setdefault("hypothesis", (
        "Caps-listicle hooks on backend/SQL/interview topics earn views; "
        "debate angles earn replies."
    ))
    state.setdefault("pivots", 0)
    state.setdefault("streak", 0)
    state.setdefault("history", [])
    return state


def save_strategy(state, path=STRATEGY_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _age_hours(post, now):
    stamp = post.get("timestamp") or ""
    try:
        born = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return 0.0
    return max(0.0, (now - born).total_seconds() / 3600)


def evaluate(posts, now=None):
    """Score recent posts. Returns dict with judged list + streak + median.

    Only posts >= JUDGE_AFTER_HOURS old count. A judged post
    underperforms when its views < UNDERPERFORM_RATIO * median.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    views = sorted(
        (views_of(p) for p in posts if (p.get("metrics") or {}).get("views") is not None)
    )
    median = views[len(views) // 2] if views else 0
    floor = max(20, int(median * UNDERPERFORM_RATIO))

    judged = []
    for post in sorted(posts, key=lambda p: p.get("timestamp") or ""):
        age = _age_hours(post, now)
        if age < JUDGE_AFTER_HOURS:
            continue
        text = (post.get("text") or "")[:160].replace("\n", " ")
        judged.append({
            "text": text,
            "views": views_of(post),
            "engagement": engagement_of(post),
            "age_h": round(age, 1),
            "under": views_of(post) < floor,
        })
    judged = judged[-LOOKBACK_POSTS:]

    streak = 0
    for item in reversed(judged):
        if item["under"]:
            streak += 1
        else:
            break

    return {
        "median": median,
        "floor": floor,
        "judged": judged,
        "streak": streak,
        "pivot": streak >= PIVOT_STREAK,
    }


def build_prompt(scorecard, strategy):
    """Strategist prompt: scorecard in, thread + hypothesis out."""
    lines = [
        f"Rolling median views: {scorecard['median']}, "
        f"underperform line: {scorecard['floor']}.",
        f"Underperform streak: {scorecard['streak']} "
        f"(pivot at {PIVOT_STREAK}).",
        "Recent judged posts (oldest first):",
    ]
    for item in scorecard["judged"][-8:]:
        flag = "UNDER" if item["under"] else "ok"
        lines.append(
            f"- [{flag}] {item['views']} views, {item['engagement']} eng "
            f"({item['age_h']}h old): {item['text']}"
        )
    lines.append(f"Current hypothesis: {strategy['hypothesis']}")
    lines.append(NICHE_PLAYBOOK)

    if scorecard["pivot"]:
        lines.append(
            "PIVOT MODE: the current strategy is failing (views below "
            "the line even after 3+ hours). Change the approach: new hook "
            "formula, topic, or format. Keep what the winners above prove, "
            "drop what the UNDER posts prove. Say what you changed in one line."
        )
    else:
        lines.append(
            "Double down on what works, vary topics so hooks never repeat. "
            "One line on why you chose this thread."
        )
    lines.append(
        "Output: first your thread as 2-7 posts separated by a line with "
        "exactly --- and nothing else (every post <=500 chars). "
        "CRITICAL: end post 1 with ' 1/N', post 2 with ' 2/N', and so on "
        "where N is the total post count — e.g. a 5-post thread ends its "
        "posts with 1/5 2/5 3/5 4/5 5/5. A post without its counter is "
        "rejected. Last post ends with a follow or comment CTA. "
        "After the posts, on its own final line: HYPOTHESIS: <one line>."
    )
    return "\n".join(lines)


def parse_decision(raw):
    """Split thread posts + trailing HYPOTHESIS line. Raises on garbage."""
    lines = [line for line in raw.strip().splitlines()]
    hypothesis = ""
    body = list(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().upper().startswith("HYPOTHESIS:"):
            hypothesis = lines[i].split(":", 1)[1].strip()
            body = lines[:i]
            break
    text = "\n".join(body)
    posts = [c.strip() for c in text.split("\n---\n") if c.strip()]
    posts = [p for p in posts if set(p) != {"-"}]
    if not 2 <= len(posts) <= 7:
        raise ValueError(
            f"strategist returned {len(posts)} usable posts, need 2-7"
        )
    return posts, hypothesis


def repair_counters(posts):
    """Append missing k/N counters. Only touches posts with no counter
    pattern at all, so model-written numbering is never double-tagged."""
    import re

    n = len(posts)
    repaired = []
    for i, post in enumerate(posts, start=1):
        if re.search(r"\d+/\d+", post):
            repaired.append(post)
        else:
            repaired.append(post.rstrip() + f" {i}/{n}")
    return repaired


def repair_thread(posts):
    """Deterministic salvage: counters + follow CTA. Appends only what
    is missing, never rewrites model content."""
    fixed = repair_counters(posts)
    if fixed:
        tail = fixed[-1].lower()
        if "follow @" not in tail and "comment" not in tail:
            fixed[-1] = fixed[-1].rstrip() + (
                "\n\nFollow @owlsky17 so you don't miss the next one."
            )
            fixed = repair_counters(fixed)
    return fixed


def soft_validate(posts):
    """Enforce only hard constraints; style is the model's playground."""
    problems = []
    n = len(posts)
    for i, post in enumerate(posts, start=1):
        if len(post) > 500:
            problems.append(f"post {i}: {len(post)} chars, max 500")
        if f"{i}/{n}" not in post:
            problems.append(f"post {i}: missing {i}/{n} counter")
    tail = posts[-1].lower() if posts else ""
    if "follow @" not in tail and "comment" not in tail:
        problems.append("last post needs a follow or comment CTA")
    return problems


def record_run(state, pillar, posts, ids, run_at, pivoted):
    state["streak"] = 0
    if pivoted:
        state["pivots"] = state.get("pivots", 0) + 1
    entry = {
        "run_at": run_at,
        "pillar": pillar,
        "hook_sig": hook_signature(posts[0]),
        "ids": ids,
        "pivoted": pivoted,
    }
    history = state.get("history", [])
    history.append(entry)
    state["history"] = history[-50:]
    save_strategy(state)
