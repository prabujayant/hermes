"""Fetch posts and rank them by views.

Post-level insights require the ``threads_manage_insights`` permission.
"""

import re
import statistics
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .client import ThreadsAPIError

ENGAGEMENT_METRICS = ("likes", "replies", "reposts", "quotes", "shares")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "to", "of", "in", "on", "for", "with", "as", "at", "by",
    "from", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "you", "your", "yours", "i", "my", "me", "mine", "we", "our", "they",
    "their", "he", "she", "his", "her", "not", "no", "do", "does", "did",
    "can", "could", "will", "would", "should", "must", "have", "has", "had",
    "how", "what", "when", "where", "which", "who", "why", "so", "up", "out",
    "about", "into", "over", "after", "just", "more", "most", "very", "also",
    "get", "got", "make", "made", "like", "one", "two", "all", "any", "some",
    "new", "use", "using", "dont", "don", "im", "ive", "youre", "thats",
    "there", "here", "them", "us", "am", "only", "even", "still", "want",
    "need", "know", "think", "going", "way", "thing", "things", "let", "lets",
    "com", "https", "http", "www",
}


def extract_metrics(payload):
    """Flatten an insights payload into {metric_name: value}."""
    metrics = {}
    for item in payload.get("data", []) or []:
        name = item.get("name")
        values = item.get("values") or []
        if not name or not values:
            continue
        value = values[0].get("value")
        if isinstance(value, (int, float)):
            metrics[name] = value
    return metrics


def extract_account_totals(payload):
    """Account-level totals: views summed across days, likes/followers
    from total_value. Returns {views, likes, followers} (None missing)."""
    totals = {"views": None, "likes": None, "followers": None}
    for item in (payload.get("data", []) or []):
        name = item.get("name")
        if name == "views":
            day_values = [v.get("value", 0)
                          for v in item.get("values", []) or []]
            day_values = [v for v in day_values
                          if isinstance(v, (int, float))]
            if day_values:
                totals["views"] = sum(day_values)
        elif name == "likes":
            value = (item.get("total_value") or {}).get("value")
            if isinstance(value, (int, float)):
                totals["likes"] = value
        elif name == "followers_count":
            value = (item.get("total_value") or {}).get("value")
            if isinstance(value, (int, float)):
                totals["followers"] = value
    return totals


def fetch_posts(client, max_posts=200, pause=0, on_progress=None, max_workers=20):
    """Fetch the account's posts, newest first, with per-post insights.

    The Threads API only exposes per-media ``GET /{id}/insights`` — there is
    no bulk endpoint, and ``graph.threads.net`` does not support Graph batch
    requests. So "all at once" means N concurrent GETs via a thread pool
    instead of N sequential GETs.
    """
    posts = []
    after = None

    while len(posts) < max_posts:
        page = client.list_posts(limit=50, after=after)
        batch = page.get("data") or []
        if not batch:
            break
        posts.extend(batch)
        after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
        if not after:
            break

    posts = posts[:max_posts]

    if not posts:
        return posts

    if max_workers <= 1:
        # Sequential fallback (e.g. for debugging / strict rate limits).
        import time

        for index, post in enumerate(posts, start=1):
            try:
                post["metrics"] = extract_metrics(client.get_post_insights(post["id"]))
            except ThreadsAPIError as exc:
                post["metrics"] = {}
                post["metrics_error"] = str(exc)
            if on_progress:
                on_progress(index, len(posts), post)
            if pause:
                time.sleep(pause)
        return posts

    lock = threading.Lock()
    done = 0

    def _fetch_one(post):
        nonlocal done
        try:
            post["metrics"] = extract_metrics(client.get_post_insights(post["id"]))
        except ThreadsAPIError as exc:
            post["metrics"] = {}
            post["metrics_error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - network errors must not abort the run
            post["metrics"] = {}
            post["metrics_error"] = str(exc)
        if on_progress:
            with lock:
                done += 1
                current = done
            on_progress(current, len(posts), post)
        return post

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_fetch_one, posts))

    return posts


def views_of(post):
    return (post.get("metrics") or {}).get("views") or 0


def engagement_of(post):
    metrics = post.get("metrics") or {}
    return sum(metrics.get(name) or 0 for name in ENGAGEMENT_METRICS)


def engagement_rate(post):
    views = views_of(post)
    if not views:
        return 0.0
    return engagement_of(post) / views


def rank_by_views(posts, top_n=10):
    """Posts that have a views value, highest first."""
    measured = [p for p in posts if (p.get("metrics") or {}).get("views") is not None]
    return sorted(measured, key=views_of, reverse=True)[:top_n]


def word_frequencies(posts):
    counter = Counter()
    for post in posts:
        words = re.findall(r"[a-z][a-z']{2,}", (post.get("text") or "").lower())
        counter.update(word for word in words if word not in STOPWORDS)
    return counter


def posting_times(posts):
    days, hours = Counter(), Counter()
    for post in posts:
        parsed = _parse_timestamp(post.get("timestamp"))
        if parsed:
            days[parsed.strftime("%A")] += 1
            hours[parsed.hour] += 1
    return days, hours


def date_range(posts):
    stamps = [_parse_timestamp(p.get("timestamp")) for p in posts]
    stamps = sorted(s for s in stamps if s)
    if not stamps:
        return None, None
    return stamps[0], stamps[-1]


def _parse_timestamp(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S+0000"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _truncate(text, width):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "\u2026"


def summarise(posts):
    measured = [p for p in posts if (p.get("metrics") or {}).get("views") is not None]
    views = [views_of(p) for p in measured]
    if not views:
        return None

    total = sum(views)
    top = max(views)
    return {
        "analysed": len(posts),
        "measured": len(measured),
        "unmeasured": len(posts) - len(measured),
        "total_views": total,
        "mean_views": total / len(views),
        "median_views": statistics.median(views),
        "max_views": top,
        "min_views": min(views),
        "top_share": (top / total * 100) if total else 0.0,
        "total_engagement": sum(engagement_of(p) for p in measured),
    }


def render_report(posts, top_n=10, username=None):
    """Build a readable text report."""
    lines = []
    summary = summarise(posts)
    if not summary:
        return (
            "No view data returned.\n"
            "Insights need the threads_manage_insights permission; re-run "
            "`threads-publish auth` after enabling it on your use case."
        )

    start, end = date_range(posts)
    header = "Threads Performance Report"
    if username:
        header += f" - @{username}"
    lines.append(header)
    lines.append("=" * len(header))
    if start and end:
        lines.append(
            f"Window: {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"
        )
    lines.append(
        f"Posts analysed: {summary['analysed']} "
        f"({summary['measured']} with view data, {summary['unmeasured']} without)"
    )
    lines.append("")

    lines.append("Overview")
    lines.append("-" * 8)
    lines.append(f"  Total views      {summary['total_views']:,}")
    lines.append(f"  Mean per post    {summary['mean_views']:,.0f}")
    lines.append(f"  Median per post  {summary['median_views']:,.0f}")
    lines.append(f"  Range            {summary['min_views']:,} - {summary['max_views']:,}")
    lines.append(f"  Top post share   {summary['top_share']:.1f}% of all views")
    lines.append(f"  Total engagement {summary['total_engagement']:,}")
    lines.append("")

    top_posts = rank_by_views(posts, top_n)
    lines.append(f"Top {len(top_posts)} posts by views")
    lines.append("-" * 24)
    lines.append(
        f"  {'#':>2}  {'views':>8}  {'eng':>5}  {'rate':>6}  {'date':<10}  post"
    )
    for position, post in enumerate(top_posts, start=1):
        parsed = _parse_timestamp(post.get("timestamp"))
        date = parsed.strftime("%Y-%m-%d") if parsed else "?"
        lines.append(
            f"  {position:>2}  {views_of(post):>8,}  {engagement_of(post):>5,}  "
            f"{engagement_rate(post) * 100:>5.1f}%  {date:<10}  "
            f"{_truncate(post.get('text'), 58)}"
        )
    lines.append("")

    if top_posts:
        best = top_posts[0]
        lines.append("Standout post")
        lines.append("-" * 13)
        lines.append(f"  {views_of(best):,} views, {engagement_of(best):,} interactions")
        lines.append(f"  {best.get('permalink', '(no permalink)')}")
        lines.append(f"  {_truncate(best.get('text'), 76)}")
        lines.append("")

    counts = Counter(p.get("media_type", "UNKNOWN") for p in posts)
    if counts:
        lines.append("Media types")
        lines.append("-" * 10)
        for media_type, count in counts.most_common():
            lines.append(f"  {media_type:<10} {count}")
        lines.append("")

    top_ids = {p["id"] for p in top_posts}
    top_words = word_frequencies([p for p in posts if p["id"] in top_ids])
    other_words = word_frequencies([p for p in posts if p["id"] not in top_ids])

    if top_words:
        lines.append(f"Themes in the top {len(top_posts)} (vs everything else)")
        lines.append("-" * 40)
        for word, count in top_words.most_common(10):
            elsewhere = other_words.get(word, 0)
            lines.append(f"  {word:<18} top {count:>3}   elsewhere {elsewhere:>3}")

    return "\n".join(lines)
