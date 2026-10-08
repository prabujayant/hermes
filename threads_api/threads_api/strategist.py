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


def ascii_clean(text):
    """Replace em/en dashes and fancy hyphens so the model never sees
    (and copies) them in scorecards, hypotheses, or examples."""
    import re

    text = re.sub(r"\s+[—–]\s+", ", ", text)
    return re.sub(r"[—–‐‑]", "-", text)


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
            f"({item['age_h']}h old): {ascii_clean(item['text'])}"
        )
    lines.append(f"Current hypothesis: {ascii_clean(strategy['hypothesis'])}")
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
