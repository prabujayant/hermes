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
NICHE_PATH = Config.ROOT / "queue" / "niche_leaders.md"
NICHE_POSTS_PATH = Config.ROOT / "queue" / "niche_posts.json"


def load_niche_brief(path=None):
    """Niche-leader playbook, editable without code changes."""
    try:
        return Path(path or NICHE_PATH).read_text(encoding="utf-8")
    except OSError:
        return ""


def load_owner_brief(path=None):
    """Owner-filled brief (bio, audience, assets, channels, constraints).

    Comment lines (#) and placeholder lines are stripped so unfilled
    sections stay silent instead of polluting the prompt.
    """
    try:
        text = Path(
            path or Config.ROOT / "queue" / "owner.md").read_text(
                encoding="utf-8")
    except OSError:
        return ""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        content = stripped[2:] if stripped.startswith("- ") else stripped
        if content.lower().startswith(("paste ", "describe ",
                                       "none listed yet")):
            continue
        if stripped.rstrip(":") in ("BIO", "AUDIENCE", "ASSETS",
                                    "CHANNELS", "CONSTRAINTS"):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def load_niche_posts(path=None, limit=8):
    """Top creator posts by views (nulls last). Each entry: creator,
    hook, text, views, likes, format, topic."""
    try:
        data = json.loads(Path(path or NICHE_POSTS_PATH).read_text(
            encoding="utf-8"))
    except (ValueError, OSError):
        return []
    entries = data.get("posts", data if isinstance(data, list) else [])
    entries = [e for e in entries if isinstance(e, dict)]
    entries.sort(key=lambda e: (e.get("views") is None,
                                -(e.get("views") or 0)))
    return entries[:limit]


def load_niche_scan(path=None, limit=10):
    """Fresh TOP-ranked public posts from keyword_search. Returns
    (fetched_iso, public_bool, items). Empty items when never scanned."""
    try:
        data = json.loads(Path(
            path or Config.ROOT / "queue" / "niche_scan.json").read_text(
                encoding="utf-8"))
    except (ValueError, OSError):
        return "", False, []
    return (data.get("fetched", ""), bool(data.get("public")),
            data.get("items", [])[:limit])

# Model chains. Brain (Luna base) decides direction; hand (Nemotron)
# writes. Free fallbacks keep posting on outage. No Pro: base proved
# equally reliable at a third of the price.
BRAIN_MODELS = [
    "openai/gpt-6-luna",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]
# Writer chain: best free voice first, paid ling-flash last resort.
SMART_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
]

# The mission, set by the account owner: 200,000 views in 30 days
# (deadline SPRINT_END) plus follower growth to match. Maximum
# aggression within honesty and platform rules: never fake numbers,
# never deceive, never spam, never harass. Win on hooks, formats,
# and volume.
VIEWS_GOAL = 200_000
FOLLOWERS_GOAL = 500
# Sprint: the account owner ordered both goals in 1 month.
SPRINT_END = datetime.date(2026, 11, 10)


def sprint_status(today=None, followers=None, views=None):
    """Pace math vs the sprint deadline. Returns dict for the prompt."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    days_left = max((SPRINT_END - today).days, 0)
    out = {"days_left": days_left, "deadline": SPRINT_END.isoformat()}
    if days_left > 0:
        if followers is not None:
            out["followers_per_day_needed"] = max(
                (FOLLOWERS_GOAL - followers) / days_left, 0)
        if views is not None:
            out["views_per_day_needed"] = max(
                (VIEWS_GOAL - views) / days_left, 0)
    else:
        out["followers_per_day_needed"] = 0
        out["views_per_day_needed"] = 0
    return out

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


def track_progress(state, totals):
    """Record goal counters, return progress dict with deltas vs last run."""
    prev_followers = state.get("last_followers")
    prev_views = state.get("last_views")
    followers = (totals or {}).get("followers")
    views = (totals or {}).get("views")
    progress = {
        "followers": followers,
        "followers_delta": (followers - prev_followers
                            if followers is not None
                            and prev_followers is not None else None),
        "views_total": views,
        "views_delta": (views - prev_views
                        if views is not None and prev_views is not None
                        else None),
    }
    if followers is not None:
        state["last_followers"] = followers
    if views is not None:
        state["last_views"] = views
    return progress


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


def build_prompt(scorecard, strategy, progress=None):
    """Strategist prompt: scorecard in, thread + hypothesis out."""
    lines = [
        f"MISSION: reach {VIEWS_GOAL:,} views and {FOLLOWERS_GOAL:,} "
        "followers. Be maximally aggressive: bold hooks, sharp debates, "
        "high-volume testing. NEVER fake numbers, deceive, spam, or "
        "harass. Win on hooks, formats, and volume.",
    ]
    if progress:
        followers = progress.get("followers")
        fdelta = progress.get("followers_delta")
        views = progress.get("views_total")
        vdelta = progress.get("views_delta")
        if followers is not None:
            fdelta_txt = f"{fdelta:+,}" if fdelta is not None else "?"
            views_txt = f"{views:,}" if views is not None else "?"
            vdelta_txt = f"{vdelta:+,}" if vdelta is not None else "?"
            lines.append(
                f"Progress: {followers:,} followers ({fdelta_txt} since "
                f"last check) of {FOLLOWERS_GOAL:,}; {views_txt} recent "
                f"views ({vdelta_txt}), goal {VIEWS_GOAL:,}. Every thread "
                "must buy reach."
            )
        pace = sprint_status(followers=followers, views=views)
        lines.append(
            f"SPRINT: {pace['days_left']} days left until {pace['deadline']}. "
            f"You need {pace.get('followers_per_day_needed', 0):,.0f} new "
            f"followers/day and {pace.get('views_per_day_needed', 0):,.0f} "
            "views/day from here. Steady singles will NOT get there. "
            "Swing for fences: pick the highest-variance play available "
            "(hot debate, breaking niche news, contrarian lesson, open "
            "loop), not the safest one. If anything broke in the niche "
            "in the last 48h (new model, outage, layoffs, viral debate), "
            "ride it this hour."
        )
    lines += [
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
    brief = load_niche_brief()
    if brief:
        lines.append(
            "NICHE LEADERS - imitate as-is. Pick ONE leader format below "
            "and copy its hook shape, structure, and pacing exactly, "
            "applied to our pillars. Do not blend formats:\n" + brief
        )
    owner = load_owner_brief()
    if owner:
        lines.append("OWNER BRIEF - facts about the account owner, use them "
                     "for positioning and never contradict them:\n" + owner)
    proof = load_niche_posts()
    if proof:
        lines.append(
            "NICHE PROOF - real posts, real numbers. The top earner sets "
            "the bar: imitate its format mechanics exactly for your "
            "pillar and topic:"
        )
        for entry in proof:
            views = entry.get("views")
            score = f"{views:,} views" if views else "views n/a"
            likes = entry.get("likes")
            if likes:
                score += f", {likes:,} likes"
            lines.append(
                f"- {entry.get('creator')}: {score} | "
                f"format {entry.get('format')} | topic {entry.get('topic')} "
                f"| hook: {ascii_clean(entry.get('hook') or '')}"
            )
    fetched, public, scan_items = load_niche_scan()
    if scan_items:
        import datetime

        try:
            age_h = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.datetime.fromisoformat(fetched)).total_seconds() / 3600
            age_note = f"{age_h:.0f}h old"
        except ValueError:
            age_note = "unknown age"
        scope = ("public TOP-ranked posts (popularity-ranked by Threads)"
                 if public else
                 "OWN posts only (Meta has not approved public search yet)")
        lines.append(
            f"LIVE NICHE SCAN ({age_note}, {scope}). Steal hooks and "
            "angles that fit your pillar:"
        )
        for item in scan_items:
            lines.append(
                f"- @{item.get('author')} via {item.get('via')}: "
                f"{ascii_clean((item.get('text') or '')[:220])}"
            )

    if scorecard["pivot"]:
        lines.append(
            "PIVOT MODE: the current strategy is failing. Change AT MOST "
            "ONE factor (topic, hook style, format, or switch to an "
            "untested niche-leader format from the brief) versus the "
            "current hypothesis, keep everything else. Explain the change "
            "in one line and cite the specific post IDs that prove it."
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
        "Output: first your thread as EXACTLY 5 posts separated by a line "
        "with exactly --- and nothing else (every post <=500 chars). "
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


def decide_direction(llm_chat, models, scorecard, strategy, progress=None,
                     max_tokens=400, temperature=0.7):
    """Brain call: strategy direction only, no thread text.

    Returns {topic, hook, change, hypothesis}. Raises ValueError when
    the response does not follow the format — caller falls back to the
    single-call path.
    """
    lines = [
        "You are the chief strategist. Decide the next thread's "
        "direction. Do NOT write the thread, only the plan.",
        f"Mission: {VIEWS_GOAL:,} views and {FOLLOWERS_GOAL:,} followers. "
        "Aggressive but honest: never fake, deceive, spam, or harass.",
    ]
    if progress and progress.get("followers") is not None:
        lines.append(
            f"Progress: {progress['followers']:,} followers, "
            f"{progress.get('views_total', '?')} recent views."
        )
    judged = scorecard.get("judged", [])
    lines.append(f"Measured posts: {scorecard.get('sample_size', len(judged))}, "
                 f"median {scorecard.get('median_vph')}/h, "
                 f"streak {scorecard.get('streak')}, "
                 f"pivot={scorecard.get('pivot')}.")
    for item in judged[-6:]:
        flag = "UNDER" if item["under"] else "ok"
        lines.append(
            f"- [{flag}] {item['views']} views ({item['vph']}/h): "
            f"{ascii_clean(item['text'][:120])}"
        )
    lines.append(f"Current hypothesis: {ascii_clean(strategy['hypothesis'])}")
    lines.append(
        "Reply with ONLY these 4 lines, no extra text:\n"
        "TOPIC: <pillar: leetcode|backend|sql|interviews|career + subject>\n"
        "HOOK: <hook formula name + one-line shape, e.g. 'debate hot take: "
        "monoliths beat microservices for most teams'>\n"
        "CHANGE: <what changes vs current hypothesis, or 'none'>\n"
        "HYPOTHESIS: <updated one-line hypothesis>"
    )
    raw = llm_chat(
        [{"role": "system",
          "content": "You decide content strategy. Terse, exact format."},
         {"role": "user", "content": "\n".join(lines)}],
        max_tokens=max_tokens, temperature=temperature, model=models)
    import re

    def field(name):
        match = re.search(rf"^{name}:\s*(.+)$", raw,
                          re.MULTILINE | re.IGNORECASE)
        if not match or not match.group(1).strip():
            raise ValueError(f"brain response missing {name} line")
        return match.group(1).strip()[:300]

    return {
        "topic": field("TOPIC"),
        "hook": field("HOOK"),
        "change": field("CHANGE"),
        "hypothesis": field("HYPOTHESIS"),
    }


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
