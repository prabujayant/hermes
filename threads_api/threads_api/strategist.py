"""Autonomous strategist: measure views, decide the next thread, pivot.

Closed loop (runs hourly, free models only — never paid in auto mode):
  1. MEASURE: fetch recent posts + insights, score posts old enough to
     judge (>= 3h), compare against the rolling median.
  2. DECIDE: one LLM call gets the scorecard + current hypothesis and
     returns the next thread plus an updated hypothesis.
  3. PIVOT: 2+ consecutive underperformers -> the prompt orders a
     strategy change (new angle/format/topic), recorded in state.

The model has freedom to experiment: only API-hard constraints are
enforced (<=500 chars, 2-7 posts, follow CTA). Hook style, topics
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
# No pivot without at least this many measured posts (small samples lie).
MIN_JUDGED = 3
# Velocity floor (views/hour) so dead-quiet accounts still get signal.
MIN_VELOCITY_FLOOR = 2.0

NICHE_PLAYBOOK = """Proven viral moves in the coding niche on Threads:
- Curiosity-gap opener (open loop, payoff inside the thread).
- Caps-listicle hook: IF YOU WANT X, STOP Y. MEMORIZE THESE N Z.
- Debate hot take ending in a challenge (highest reply rates).
- Contrarian lessons: 'most X advice is outdated, here is what changed'.
- Cheatsheet/stack thread with a strong payoff post.
- Algorithm facts: replies (esp. replies-to-replies in the first hours),
  profile clicks and view-time drive reach. One topic tag per post.
House rules (non-negotiable): plain ASCII text only. NEVER use em
dashes or en dashes, use commas or plain hyphens. The final post is
ALWAYS the follow CTA for @owlsky17, never a comment CTA."""


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
    """Score recent posts fairly. Returns dict with judged list + verdict.

    Fairness rules (scorecard trustworthiness):
    - Posts WITHOUT valid view data are excluded entirely (never flops).
    - Posts younger than JUDGE_AFTER_HOURS are excluded (too early).
    - Comparison uses views-PER-HOUR velocity, so a 4h post and a 20h
      post are judged on the same scale instead of raw totals.
    - Priority: reach (velocity) first; engagement rate breaks ties in
      the prompt, not the score.
    - A pivot needs MIN_JUDGED measured posts AND a PIVOT_STREAK of
      consecutive underperformers (no strategy change on small samples).
    """
    from .insights import engagement_rate

    now = now or datetime.datetime.now(datetime.timezone.utc)
    measured = [
        p for p in posts
        if (p.get("metrics") or {}).get("views") is not None
        and _age_hours(p, now) >= JUDGE_AFTER_HOURS
    ]
    velocities = sorted(
        views_of(p) / max(_age_hours(p, now), 0.1) for p in measured
    )
    median_vph = velocities[len(velocities) // 2] if velocities else 0.0
    floor_vph = max(MIN_VELOCITY_FLOOR, median_vph * UNDERPERFORM_RATIO)

    judged = []
    for post in sorted(measured, key=lambda p: p.get("timestamp") or ""):
        age = _age_hours(post, now)
        vph = views_of(post) / max(age, 0.1)
        metrics = post.get("metrics") or {}
        text = (post.get("text") or "")[:160].replace("\n", " ")
        judged.append({
            "views": views_of(post),
            "vph": round(vph, 1),
            "likes": metrics.get("likes", 0),
            "replies": metrics.get("replies", 0),
            "reposts": metrics.get("reposts", 0),
            "rate_pct": round(engagement_rate(post) * 100, 2),
            "engagement": engagement_of(post),
            "age_h": round(age, 1),
            "text": text,
            "under": vph < floor_vph,
        })
    judged = judged[-LOOKBACK_POSTS:]
    for i, item in enumerate(judged, start=1):
        item["id"] = f"P{i}"

    streak = 0
    for item in reversed(judged):
        if item["under"]:
            streak += 1
        else:
            break

    ready = len(judged) >= MIN_JUDGED
    return {
        "median_vph": round(median_vph, 1),
        "floor_vph": round(floor_vph, 1),
        "judged": judged,
        "sample_size": len(judged),
        "streak": streak,
        "ready": ready,
        "pivot": ready and streak >= PIVOT_STREAK,
    }


def ascii_clean(text):
    """Replace em/en dashes and fancy hyphens so the model never sees
    (and copies) them in scorecards, hypotheses, or examples."""
    import re

    text = re.sub(r"\s+[—–]\s+", ", ", text)
    return re.sub(r"[—–‐‑]", "-", text)


def build_prompt(scorecard, strategy):
    """Strategist prompt: scorecard in, thread + hypothesis out."""
    lines = [
        f"Measured posts: {scorecard['sample_size']} "
        f"(need {MIN_JUDGED} for strategy changes). "
        f"Median velocity: {scorecard['median_vph']} views/h, "
        f"underperform line: {scorecard['floor_vph']} views/h. "
        "Reach decides; engagement rate only breaks ties.",
        f"Underperform streak: {scorecard['streak']} "
        f"(pivot at {PIVOT_STREAK}).",
        "Recent judged posts, oldest first (cite IDs like P3):",
    ]
    for item in scorecard["judged"][-8:]:
        flag = "UNDER" if item["under"] else "ok"
        lines.append(
            f"- {item['id']} [{flag}] {item['views']} views "
            f"({item['vph']}/h, {item['age_h']}h old), "
            f"likes {item['likes']}, replies {item['replies']}, "
            f"reposts {item['reposts']}, rate {item['rate_pct']}%: "
            f"{ascii_clean(item['text'])}"
        )
    lines.append(f"Current hypothesis: {ascii_clean(strategy['hypothesis'])}")
    lines.append(NICHE_PLAYBOOK)

    if scorecard["pivot"]:
        lines.append(
            "PIVOT MODE: the current strategy is failing. Change AT MOST "
            "ONE factor (topic, hook style, or format) versus the current "
            "hypothesis, keep everything else. Explain the change in one "
            "line and cite the specific post IDs that prove it."
        )
    elif not scorecard["ready"]:
        lines.append(
            "WARM-UP: too few measured posts to judge strategy. Post your "
            "best guess from the playbook and say which factor you chose."
        )
    else:
        lines.append(
            "Double down on what works, vary topics so hooks never repeat. "
            "Change at most one factor. One line on why, citing post IDs."
        )
    lines.append(
        "Output: first your thread as 2-7 posts separated by a line with "
        "exactly --- and nothing else (every post <=500 chars). "
        "CRITICAL: do NOT number the posts, do NOT write counters like "
        "1/5 or 2/5 anywhere, just write the post text. Use plain ASCII "
        "only: no em dashes, no en dashes, no fancy unicode, commas and "
        "plain hyphens only. The LAST post is always exactly the follow "
        "CTA: 'Follow @owlsky17 so you don't miss the next one.' "
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


def repair_thread(posts):
    """Deterministic salvage: ASCII dashes, no counters, follow CTA.

    Strips any k/N counters the model added anyway, swaps non-ASCII
    dashes, appends the follow CTA if missing. Never rewrites content.
    """
    import re

    fixed = []
    for post in posts:
        post = ascii_clean(post)
        post = re.sub(r"\s*\d+/\d+\s*$", "", post).rstrip()
        fixed.append(post)
    if fixed:
        tail = fixed[-1].lower()
        if "follow @" not in tail:
            stripped = fixed[-1].rstrip()
            fixed[-1] = (
                stripped + "\n\nFollow @owlsky17 so you don't "
                "miss the next one."
            )
    return fixed


def finalize_thread(raw):
    """Parse + repair + validate a model response. Returns (posts, hyp).
    Raises ValueError with the reason on any failure."""
    posts, hypothesis = parse_decision(raw)
    posts = repair_thread(posts)
    problems = soft_validate(posts)
    if problems:
        raise ValueError("; ".join(problems))
    return posts, hypothesis


def soft_validate(posts):
    """Enforce only hard constraints; style is the model's playground."""
    problems = []
    for i, post in enumerate(posts, start=1):
        if len(post) > 500:
            problems.append(f"post {i}: {len(post)} chars, max 500")
        if ascii_clean(post) != post:
            problems.append(f"post {i}: non-ASCII dashes present")
    for post in posts:
        import re

        if re.search(r"\d+/\d+", post):
            problems.append("counters like 1/5 are not allowed")
            break
    tail = posts[-1].lower() if posts else ""
    if "follow @" not in tail:
        problems.append("last post must be the follow CTA for @owlsky17")
    return problems


def record_run(state, pillar, posts, ids, run_at, pivoted, model=None,
               path=STRATEGY_PATH):
    """Save draft + outcome accountability: full texts, model, hypothesis.

    Later runs re-fetch live metrics for these IDs, so each entry links
    the decision (texts/model/hypothesis) to its eventual performance.
    """
    state["streak"] = 0
    if pivoted:
        state["pivots"] = state.get("pivots", 0) + 1
    entry = {
        "run_at": run_at,
        "pillar": pillar,
        "hook_sig": hook_signature(posts[0]),
        "texts": list(posts),
        "model": model,
        "hypothesis": (state.get("hypothesis") or "")[:300],
        "ids": ids,
        "pivoted": pivoted,
    }
    history = state.get("history", [])
    history.append(entry)
    state["history"] = history[-50:]
    save_strategy(state, path)
