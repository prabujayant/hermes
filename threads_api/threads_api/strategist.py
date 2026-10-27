"""Autonomous strategist: measure views, decide the next thread, pivot.

Closed loop (runs on schedule, currently ~every 30 min; Luna base
decides, Nemotron writes, free fallbacks behind both):
  1. MEASURE: fetch recent posts + insights, score posts old enough to
     judge (>= 3h), compare against the rolling median.
  2. DECIDE: one LLM call gets the scorecard + current hypothesis and
     returns the next thread plus an updated hypothesis.
  3. PIVOT: 2+ consecutive underperformers -> the prompt orders a
     strategy change (new angle/format/topic), recorded in state.

The model chooses topics and angles, but the owner has locked the
format: queue/STYLE.md, derived from the 50 all-time most-viewed posts,
controls hook shapes, thread structure, and the CTA. Pivots may change
only the subject or angle, never the format. API-hard constraints are
also enforced (<=500 chars, 2-7 posts, follow CTA).
"""

import datetime
import json
from collections import Counter
from pathlib import Path

from .config import Config
from .generator import hook_signature
from .insights import engagement_of, views_of

STRATEGY_PATH = Config.ROOT / "queue" / "strategy.json"
NICHE_PATH = Config.ROOT / "queue" / "niche_leaders.md"
NICHE_POSTS_PATH = Config.ROOT / "queue" / "niche_posts.json"
STYLE_PATH = Config.ROOT / "queue" / "STYLE.md"
TOP_ALL_TIME_PATH = Config.ROOT / "queue" / "top_all_time.json"

# The account's niche. Every thread must live inside one of these.
# Anything else (politics, crypto, general motivation, unrelated
# topics) is rejected before publishing, no matter how viral it
# looks elsewhere.
ALLOWED_PILLARS = ("leetcode", "backend", "sql", "interviews", "career",
                   "ai-coding", "system-design")


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


def load_style_guide(path=None):
    """Locked house style, derived from the all-time top 50.

    This is the owner's permanent style lock: the hourly generator must
    read it on every run, not just the manual ``write-thread`` command.
    """
    try:
        text = Path(path or STYLE_PATH).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip()


def load_top_all_time(limit=10, path=None):
    """Durable cache of the 50 most-viewed posts, ranked by views.

    The hourly run must not re-fetch 500 posts to rediscover its best
    formats. This file is a snapshot; refresh it deliberately when the
    all-time ranking changes.
    """
    try:
        data = json.loads(Path(path or TOP_ALL_TIME_PATH).read_text(
            encoding="utf-8"))
    except (ValueError, OSError):
        return []
    posts = data.get("posts", []) if isinstance(data, dict) else data
    posts = [p for p in posts if isinstance(p, dict)]
    posts.sort(key=lambda p: -(p.get("views") or 0))
    return posts[:limit]


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
LOOKBACK_POSTS = 30
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
        try:
            born = datetime.datetime.strptime(
                post.get("timestamp") or "", "%Y-%m-%dT%H:%M:%S%z")
            day = born.strftime("%Y-%m-%d")
        except ValueError:
            day = ""
        judged.append({
            "date": day,
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

    posts_24h = sum(1 for p in posts if _age_hours(p, now) <= 24)
    ready = len(judged) >= MIN_JUDGED
    return {
        "median_vph": round(median_vph, 1),
        "floor_vph": round(floor_vph, 1),
        "judged": judged,
        "all_time": all_time_winners(posts, now=now),
        "sample_size": len(judged),
        "posts_24h": posts_24h,
        "streak": streak,
        "ready": ready,
        "pivot": ready and streak >= PIVOT_STREAK,
    }


def classify_hook_type(text):
    """Crude but stable format label: caps-listicle, question/debate,
    story, or statement. Drives the follow-winning-types rule."""
    text = text or ""
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return "caps-listicle"
    if "?" in text:
        return "question/debate"
    first = text.strip().split()
    if first and first[0].lower() in ("i", "my", "we", "our"):
        return "story"
    return "statement"


FORMAT_TARGETS = {
    "caps_if_you_want": 4,
    "caps_things": 3,
    "sentence_statement": 2,
    "sentence_story": 1,
}
FORMAT_LABELS = {
    "caps_if_you_want": "CAPS IF YOU WANT",
    "caps_things": "CAPS THINGS",
    "sentence_statement": "sentence-case statement",
    "sentence_story": "sentence-case story",
}
TOPIC_TARGETS = {
    "career": 4,
    "system-design": 3,
    "sql": 2,
    "flex": 1,
}
TOPIC_LABELS = {
    "career": "career/senior",
    "system-design": "system design/interviews",
    "sql": "SQL/Postgres/ORM",
    "flex": "backend/LeetCode flex",
}
TOPIC_KEYWORDS = {
    "career": (
        "senior", "career", "hiring", "resume", "manager", "years",
        "engineer faster", "promotion", "job", "salary", "layoff",
        "on-call", "team", "interviewer",
    ),
    "sql": (
        "postgres", "sql", "orm", "uuid", "ulid", "ksuid", "index",
        "indexes", "database", "query", "queries", "foreign key",
        "redis", "innodb", "transaction", "jsonb", "bigint",
        "buffer pool", "b-tree", "table",
    ),
    "flex": (
        "leetcode", "dynamic programming", "coding interview",
        "algorithm", "data structure", "ai ", "ai-", "coding assistant",
        "copilot", "llm", "agent", "ai-written", "ai-generated",
    ),
    "system-design": (
        "system design", "interview", "interviews", "design", "kafka",
        "architecture", "scalab", "load balanc", "microservice",
        "monolith",
    ),
}


def classify_locked_format(text):
    """Map a hook to one of the four locked formats."""
    text = (text or "").strip()
    upper = text.upper()
    letters = [c for c in text if c.isalpha()]
    caps = (
        bool(letters)
        and sum(c.isupper() for c in letters) / len(letters) > 0.7
    )
    if caps:
        if upper.startswith("THINGS"):
            return "caps_things"
        if upper.startswith(("IF YOU WANT", "STOP")):
            return "caps_if_you_want"
        return "caps_if_you_want"
    first = text.split()
    if first and first[0].lower() in ("i", "my", "we", "our"):
        return "sentence_story"
    return "sentence_statement"


def classify_topic_family(text):
    """Primary topic family for style-balance accounting."""
    lowered = (text or "").lower()
    for topic in ("career", "sql", "flex", "system-design"):
        if any(needle in lowered for needle in TOPIC_KEYWORDS[topic]):
            return topic
    return "flex"


def style_balance(strategy, window=10):
    """Recommend this hour's locked slot from the last N threads.

    The account drifts when the model repeats its latest winning
    template. This line makes the 7/3 format mix and 4/3/2/1 topic mix
    explicit instead of relying on the model to count.
    """
    history = [
        entry for entry in strategy.get("history", [])
        if isinstance(entry, dict) and entry.get("texts")
    ]
    recent = history[-window:]
    count = len(recent)
    if not count:
        return (
            "STYLE BALANCE: no posted history yet. Start with CAPS IF YOU "
            "WANT on career/senior, then follow the 10-thread rotation in "
            "HOUSE STYLE."
        )
    formats = Counter(
        classify_locked_format(entry["texts"][0]) for entry in recent
    )
    topics = Counter(
        classify_topic_family(entry["texts"][0]) for entry in recent
    )

    def pick(targets, counts):
        return max(
            targets,
            key=lambda key: (
                targets[key] * count / 10 - counts.get(key, 0),
                -counts.get(key, 0),
            ),
        )

    fmt = pick(FORMAT_TARGETS, formats)
    topic = pick(TOPIC_TARGETS, topics)
    fmt_txt = ", ".join(
        f"{FORMAT_LABELS[key]} {formats.get(key, 0)}"
        for key in FORMAT_TARGETS
    )
    topic_txt = ", ".join(
        f"{TOPIC_LABELS[key]} {topics.get(key, 0)}"
        for key in TOPIC_TARGETS
    )
    return (
        f"STYLE BALANCE (last {count} threads): formats {fmt_txt}; topics "
        f"{topic_txt}. This hour's slot: {FORMAT_LABELS[fmt]} on "
        f"{TOPIC_LABELS[topic]}. The TOPIC must stay in that family and the "
        "HOOK must use that format. Pick a fresh subject inside it; do not "
        "repeat the last subject."
    )


def all_time_winners(posts, now=None, limit=6):
    """Top posts by raw views across everything fetched, any age over
    3h. These are the proven types the loop must imitate."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    ranked = []
    for post in posts:
        metrics = post.get("metrics") or {}
        if metrics.get("views") is None:
            continue
        if _age_hours(post, now) < JUDGE_AFTER_HOURS:
            continue
        text = (post.get("text") or "")[:140].replace("\n", " ")
        ranked.append({
            "views": views_of(post),
            "type": classify_hook_type(post.get("text") or ""),
            "text": text,
        })
    ranked.sort(key=lambda item: -item["views"])
    return ranked[:limit]


def _stem(word):
    """Naive suffix strip so fix/fixes and verify/verifying match."""
    for suffix in ("ing", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) > 3:
            return word[: -len(suffix)]
    return word


def _content_words(text):
    """Lowercase stemmed content words for topic comparison."""
    import re

    from .insights import STOPWORDS

    words = re.findall(r"[a-z][a-z']{2,}", (text or "").lower())
    return {_stem(w) for w in words if w not in STOPWORDS}


def topic_overlap(hook_a, hook_b):
    """Jaccard similarity of content words (0..1). Catches same-topic
    rewrites that exact-signature dedup misses."""
    set_a, set_b = _content_words(hook_a), _content_words(hook_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def hook_too_similar(hook, recent_hooks, threshold=0.3):
    """True when the hook shares too much topic with recent hooks."""
    for other in recent_hooks:
        if topic_overlap(hook, other) >= threshold:
            return True
    return False


def hook_is_live(client, hook_text, limit=10):
    """True when a post with the same hook signature is already live.

    Catches every duplication path: overlapping runs with stale state,
    retries after ambiguous failures, and bank replays after lost
    receipts. Network errors return False (fail open: a missed duplicate
    beats a skipped hour).
    """
    try:
        page = client.list_posts(limit=limit)
    except Exception:
        return False
    sig = hook_signature(hook_text or "")
    for post in page.get("data", []):
        if hook_signature(post.get("text") or "") == sig and sig:
            return True
    return False


def ascii_clean(text):
    """Replace em/en dashes and fancy hyphens so the model never sees
    (and copies) them in scorecards, hypotheses, or examples."""
    import re

    text = re.sub(r"\s+[—–]\s+", ", ", text)
    return re.sub(r"[—–‐‑]", "-", text)


def read_pillar_map(path=None):
    """Map published post id -> pillar from posted.jsonl receipts."""
    receipts = path or Config.ROOT / "queue" / "posted.jsonl"
    id_to_pillar = {}
    try:
        with open(receipts, encoding="utf-8") as handle:
            for line in handle:
                try:
                    receipt = json.loads(line)
                except ValueError:
                    continue
                for pid in receipt.get("ids", []):
                    id_to_pillar[pid] = receipt.get("pillar", "?")
    except OSError:
        pass
    return id_to_pillar


def pillar_table(posts, pillar_map=None):
    """Median velocity per pillar from live numbers. Posts with no
    matching receipt (too old) group under '?'. Pillars with <2
    measured posts are omitted (noise)."""
    import statistics

    if pillar_map is None:
        pillar_map = read_pillar_map()
    buckets = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    for post in posts:
        metrics = post.get("metrics") or {}
        if metrics.get("views") is None:
            continue
        age = _age_hours(post, now)
        if age < JUDGE_AFTER_HOURS:
            continue
        pillar = pillar_map.get(post.get("id"), "?")
        buckets.setdefault(pillar, []).append(
            views_of(post) / max(age, 0.1))
    table = {}
    for pillar, speeds in buckets.items():
        if len(speeds) >= 2:
            table[pillar] = {"n": len(speeds),
                             "med_vph": round(statistics.median(speeds), 1)}
    return table


def follower_trail(strategy, points=7):
    """Recent follower snapshots from run history, oldest first."""
    trail = [h.get("followers") for h in strategy.get("history", [])
             if isinstance(h, dict) and h.get("followers") is not None]
    return trail[-points:]


def analytics_digest(scorecard):
    """Compact account-analytics block: daily view trajectory plus the
    best and worst judged posts with full numbers."""
    judged = scorecard.get("judged", [])
    if not judged:
        return ""
    days = {}
    for item in judged:
        if item.get("date"):
            days[item["date"]] = days.get(item["date"], 0) + item["views"]
    lines = ["ANALYTICS (your full account data, use it):"]
    if days:
        trail = ", ".join(f"{day}: {total:,} views"
                          for day, total in sorted(days.items())[-5:])
        lines.append(f"Daily views trajectory: {trail}.")
    ranked = sorted(judged, key=lambda i: i["vph"])
    if ranked:
        lo, hi = ranked[0], ranked[-1]
        lines.append(
            f"Weakest: {lo['views']:,} views ({lo['vph']}/h, "
            f"{lo['likes']} likes, {lo['replies']} replies): "
            f"{ascii_clean(lo['text'][:100])}."
        )
        lines.append(
            f"Strongest: {hi['views']:,} views ({hi['vph']}/h, "
            f"{hi['likes']} likes, {hi['replies']} replies, "
            f"rate {hi['rate_pct']}%): {ascii_clean(hi['text'][:100])}."
        )
    return "\n".join(lines)


def build_prompt(scorecard, strategy, progress=None, posts=None):
    """Strategist prompt: scorecard in, thread + hypothesis out."""
    lines = [
        f"MISSION: reach {VIEWS_GOAL:,} views and {FOLLOWERS_GOAL:,} "
        "followers. Be maximally aggressive inside the locked house style: "
        "proven hooks, sharper angles, high-volume testing. NEVER fake "
        "numbers, deceive, spam, or harass. Win on hooks, formats, and "
        "volume.",
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
            "Swing for fences inside the locked formats: pick the "
            "highest-variance subject that fits the house style (career, "
            "system design, SQL/Postgres), not the safest one. A sharper "
            "angle on a proven format beats a new format. If anything broke "
            "in the niche in the last 48h (new model, outage, layoffs, viral "
            "debate), ride it this hour only if it fits the locked style."
        )
    lines += [
        f"Measured posts: {scorecard['sample_size']} "
        f"(need {MIN_JUDGED} for strategy changes). "
        f"Median velocity: {scorecard['median_vph']} views/h, "
        f"underperform line: {scorecard['floor_vph']} views/h. "
        f"Posting cadence: {scorecard.get('posts_24h', '?')} threads in "
        "the last 24h — pace your experiments to it (more posts means "
        "faster learning, each post must still earn its slot). "
        "Reach decides; engagement rate only breaks ties.",
        f"Underperform streak: {scorecard['streak']} "
        f"(pivot at {PIVOT_STREAK}).",
        "Recent judged posts, oldest first (cite IDs like P3):",
    ]
    all_time = scorecard.get("all_time", [])
    if all_time:
        lines.append(
            "ALL-TIME WINNERS (recent measured sample - new threads must "
            "match one of these types; name which in CHANGE). The locked "
            "all-time top 50 reference below is authoritative when this "
            "sample disagrees:"
        )
        for winner in all_time:
            lines.append(
                f"- {winner['views']:,} views [{winner['type']}]: "
                f"{ascii_clean(winner['text'])}"
            )
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
    lines.append(
        "TAKEOVER STANDING ORDERS (owner-mandated, every hour): "
        "1. One unusually useful original post beats filler: a sharp "
        "hook plus a concrete takeaway, example, or diagram. "
        "2. Think in series: when a topic pops, plan follow-ups that "
        "deepen it; value posts may end with a question people want to "
        "answer, but the final post is always the follow CTA. "
        "3. Replies earn reach: favor angles that invite specific "
        "comments, and keep the follow CTA earning the follow. "
        "4. Seven-day tests only: double down solely on posts beating "
        "usual reach and replies; kill the rest. "
        "5. Watch for collab openings with relevant creators (flag them "
        "in your hypothesis line; the owner negotiates)."
    )
    digest = analytics_digest(scorecard)
    if digest:
        lines.append(digest)
    if posts is not None:
        table = pillar_table(posts)
        if table:
            cells = ", ".join(
                f"{pillar}: {info['n']} posts med {info['med_vph']}/h"
                for pillar, info in sorted(
                    table.items(), key=lambda kv: -kv[1]["med_vph"]))
            lines.append(
                "PILLAR TABLE (live median velocity per pillar — "
                f"favor winners, starve losers): {cells}."
            )
    trail = follower_trail(strategy)
    if len(trail) >= 2:
        lines.append(
            "Follower trail (recent runs, oldest first): "
            + ", ".join(str(v) for v in trail) + ". Growing means the "
            "current mix converts; flat means reach is not converting."
        )
    history = [h for h in strategy.get("history", []) if isinstance(h, dict)]
    if history:
        lines.append("Your recent decisions and what they were:")
        for entry in history[-3:]:
            hook = entry.get("hook_sig", "?")
            model = (entry.get("model") or "?").split("/")[-1][:28]
            stamp = (entry.get("run_at") or "")[:16]
            lines.append(f"- {stamp} [{model}]: {hook}")
    lines.append(NICHE_PLAYBOOK)
    brief = load_niche_brief()
    if brief:
        lines.append(
            "NICHE LEADERS - mine for topics, angles, and freshness only. "
            "HOUSE STYLE controls format, hook shape, and thread structure; "
            "where this brief conflicts, HOUSE STYLE wins:\n" + brief
        )
    owner = load_owner_brief()
    if owner:
        lines.append("OWNER BRIEF - facts about the account owner, use them "
                     "for positioning and never contradict them:\n" + owner)
    proof = load_niche_posts()
    if proof:
        lines.append(
            "NICHE PROOF - real posts, real numbers. Use these as topic and "
            "angle evidence only; the locked HOUSE STYLE controls format "
            "mechanics:"
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
            "ONE factor: the topic or angle. The house style is LOCKED: "
            "same four hook formulas, same 5-post structure, same CTA, "
            "even on a pivot. Do not switch to debate, question, or an "
            "untested format. Explain the topic change in one line and cite "
            "the specific post IDs that prove it."
        )
    elif not scorecard["ready"]:
        lines.append(
            "WARM-UP: too few measured posts to judge strategy. Post your "
            "best guess inside the locked style and say which topic family "
            "you chose."
        )
    else:
        lines.append(
            "Double down on what works, vary topics so hooks never repeat. "
            "Change at most one factor (topic or angle); format stays "
            "locked. One line on why, citing post IDs."
        )
    proof = load_top_all_time(10)
    if proof:
        lines.append(
            "LOCKED TOP-10 ALL-TIME HOOKS (reference only; never reuse or "
            "closely paraphrase these lines. Copy the shape, not the words):"
        )
        for item in proof:
            lines.append(
                f"- {item.get('views', 0):,} views "
                f"[{item.get('format', '?')} / {item.get('topic', '?')}]: "
                f"{ascii_clean((item.get('text') or '')[:220])}"
            )
    style = load_style_guide()
    if style:
        lines.append(
            "HOUSE STYLE - LOCKED FROM THE 50 ALL-TIME MOST-VIEWED POSTS. "
            "This is not a suggestion. Topic and angle may vary; format may "
            "not. Where any instruction above conflicts with this section, "
            "this section wins:\n" + style
        )
    lines.append(
        "Output: first your thread as EXACTLY 5 posts separated by a line "
        "with exactly --- and nothing else (every post <=500 chars). Post 1 "
        "is one locked hook formula. Posts 2-4 are value posts. Post 5 is "
        "the follow CTA. CRITICAL: do NOT number the posts, do NOT write "
        "counters like 1/5 or 2/5 anywhere, just write the post text. Use "
        "plain ASCII only: no em dashes, no en dashes, no fancy unicode, "
        "commas and plain hyphens only. The LAST post must contain the "
        "follow CTA: 'Follow @owlsky17 so you don't miss the next one.' "
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
        "YOUR JOB: choose the topic and angle. The house style is LOCKED "
        "by the owner and is not yours to change: no new hook shape, no "
        "new format, no new thread structure. Pick the subject inside the "
        "locked mix. PIVOT means change the subject or angle, never the "
        "format. You still decide WHEN to post (answer NO to sit out a "
        "dead hour). TAKEOVER ORDERS: prefer one unusually useful original "
        "post over filler; think in follow-up series; favor angles that "
        "invite specific comments.",
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
    balance = style_balance(strategy)
    if balance:
        lines.append(balance)
    if scorecard.get("pivot"):
        recent = [ascii_clean(i["text"][:70]) for i in judged[-3:]]
        lines.append(
            "PIVOT MODE CONFIRMED. Your last directions kept the same "
            "topic family: " + " | ".join(recent) + ". "
            "This run changes the topic or angle only. The locked formats "
            "are still mandatory: choose a caps listicle or a sentence-case "
            "technical post from HOUSE STYLE. Do not change the format to "
            "force novelty. Different subject family, same winning shape."
        )
    lines.append(
        "Reply with ONLY these lines, no extra text:\n"
        "POST: <YES post this hour, or NO skip with one-line reason, e.g. "
        "dead traffic window or nothing worth saying>\n"
        "TOPIC: <pillar + subject. Home pillars: leetcode|backend|sql|"
        "interviews|career|ai-coding|system-design. BREAKOUT: if you are "
        "highly confident something OUTSIDE these pillars will go viral, "
        "use it, but then BREAKOUT below is REQUIRED>\n"
        "BREAKOUT: <none, or: why this off-pillar topic will go viral + "
        "which niche-proof entry it imitates>\n"
        "HOOK: <locked format + one-line shape. Use 'CAPS IF YOU WANT: ...', "
        "'CAPS THINGS: ...', 'SENTENCE STATEMENT: ...', or "
        "'SENTENCE STORY: ...'>\n"
        "CHANGE: <topic or angle change vs current hypothesis, or 'none'. "
        "Format changes are forbidden>\n"
        "HYPOTHESIS: <updated one-line hypothesis>\n"
        "Style is LOCKED: only the four formulas in HOUSE STYLE, always 5 "
        "posts (hook, 3 value, CTA), always follow CTA last. Plain ASCII, "
        "no counters, no new formats."
    )
    proof = load_top_all_time(10)
    if proof:
        lines.append(
            "TOP-10 ALL-TIME PROOF (reference only; never reuse these "
            "hooks or their opening phrases):"
        )
        for item in proof:
            lines.append(
                f"- {item.get('views', 0):,} views "
                f"[{item.get('format', '?')} / {item.get('topic', '?')}]: "
                f"{ascii_clean((item.get('text') or '')[:180])}"
            )
    style = load_style_guide()
    if style:
        lines.append(
            "HOUSE STYLE - LOCKED FROM THE 50 ALL-TIME MOST-VIEWED POSTS. "
            "Topic and angle may vary; format may not. Where any instruction "
            "above conflicts with this section, this section wins:\n" + style
        )
    lines.append(
        "FINAL FORMAT REMINDER: return ONLY the lines POST, TOPIC, BREAKOUT, "
        "HOOK, CHANGE, HYPOTHESIS. No thread text. The HOOK line must name "
        "one of the four locked formats."
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

    post_raw = field("POST")
    topic = field("TOPIC")
    pillar = topic.strip().split()[0].lower().rstrip(":") \
        if topic.strip() else ""
    breakout = ""
    try:
        breakout = field("BREAKOUT")
    except ValueError:
        breakout = ""
    if pillar not in ALLOWED_PILLARS:
        if not breakout or breakout.lower().startswith("none"):
            raise ValueError(
                f"topic pillar '{pillar}' is outside the niche "
                f"{'/'.join(ALLOWED_PILLARS)} without a BREAKOUT case"
            )
    return {
        "post": not post_raw.upper().startswith("NO"),
        "post_reason": post_raw,
        "pillar": pillar,
        "breakout": breakout,
        "topic": topic,
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


def follower_stall(state, now=None, days=7, min_gain=5):
    """True when follower growth is flat over the trailing window.

    Luna's prescribed second trigger: views can pass the floor while the
    account itself stalls. Reads follower snapshots from run history.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=days)
    points = []
    for entry in state.get("history", []):
        if not isinstance(entry, dict):
            continue
        followers = entry.get("followers")
        stamp = entry.get("run_at", "")
        try:
            moment = datetime.datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if followers is not None and moment >= cutoff:
            points.append(followers)
    if len(points) < 2:
        return False
    return max(points) - min(points) < min_gain


def record_run(state, pillar, posts, ids, run_at, pivoted, model=None,
               breakout="", followers=None, views_total=None,
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
        "breakout": (breakout or "")[:300],
        "followers": followers,
        "views_total": views_total,
        "ids": ids,
        "pivoted": pivoted,
    }
    history = state.get("history", [])
    history.append(entry)
    state["history"] = history[-50:]
    save_strategy(state, path)
