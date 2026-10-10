import argparse
import json
import sys

import requests

from . import llm as llm_module
from .auth import save_credentials, start_oauth_flow
from .client import ThreadsAPIError, ThreadsClient
from .config import Config
from .diagnose import run_checks
from .generator import (
    POSTED_LOG,
    choose_template,
    hook_signature,
    load_state,
    load_templates,
    mark_used,
    save_state,
    validate_thread,
)
from .insights import (
    engagement_of,
    engagement_rate,
    fetch_posts,
    render_report,
    views_of,
    word_frequencies,
)
from .llm import LLMError, chat


def _cmd_auth(args):
    if args.redirect_uri:
        Config.OAUTH_REDIRECT_URI = args.redirect_uri

    result = start_oauth_flow(open_browser=not args.no_browser)
    path = save_credentials(result["access_token"], result["user_id"])
    username = result.get("username") or "unknown"
    print(f"\nConnected as @{username} (id {result['user_id']})")
    print(f"Credentials saved to {path}")
    print("Verify with: python -m threads_api.cli verify")


def _cmd_verify(_args):
    client = ThreadsClient()
    profile = client.get_profile()
    print("Connected successfully.")
    print(f"  id:       {profile.get('id')}")
    print(f"  username: @{profile.get('username')}")
    if profile.get("name"):
        print(f"  name:     {profile['name']}")


def _cmd_doctor(_args):
    results = run_checks()
    width = max(len(name) for name, _, _ in results)
    marker = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}

    for name, status, detail in results:
        print(f"{marker.get(status, '[????]')} {name.ljust(width)}  {detail}")

    return 1 if any(status == "FAIL" for _, status, _ in results) else 0


def _cmd_top_posts(args):
    client = ThreadsClient()

    def progress(done, total, post):
        metrics = post.get("metrics") or {}
        views = metrics.get("views")
        shown = f"{views:,}" if isinstance(views, (int, float)) else "-"
        print(f"  [{done}/{total}] views={shown}", file=sys.stderr)

    posts = fetch_posts(
        client,
        max_posts=args.max_posts,
        max_workers=args.workers,
        on_progress=progress if args.progress else None,
    )

    username = None
    if Config.ACCESS_TOKEN:
        try:
            username = client.get_profile().get("username")
        except ThreadsAPIError:
            username = None

    print(render_report(posts, top_n=args.limit, username=username))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(posts, handle, indent=2)
        print(f"\nRaw data written to {args.json_out}")

    return 0


def _cmd_delete(args):
    client = ThreadsClient()
    result = client.delete_post(args.post_id)
    print(f"Deleted: {result.get('deleted_id', args.post_id)}")


SCAN_AUTHORS = ["theprimeagen", "mattpocockuk", "codingwithroby",
                "sahilbloom"]
SCAN_QUERIES = ["backend engineering", "SQL", "coding interviews",
                "leetcode", "AI agents", "vibe coding"]


def _cmd_scan_niche(args):
    import datetime

    client = ThreadsClient()
    try:
        me = client.get_profile().get("username", "")
    except ThreadsAPIError as exc:
        raise RuntimeError(
            f"Could not read profile ({exc}). If this is a permissions "
            "error, re-run `auth` to grant the threads_keyword_search "
            "scope, then request Meta approval for it (see README)."
        )
    items = {}
    jobs = [("author:" + a, {"q": "software", "author_username": a})
            for a in (args.authors.split(",") if args.authors else SCAN_AUTHORS)]
    jobs += [(q, {"q": q}) for q in SCAN_QUERIES]
    for label, params in jobs:
        try:
            page = client.keyword_search(limit=args.limit, **params)
        except ThreadsAPIError as exc:
            print(f"scan {label}: {exc}")
            continue
        for post in page.get("data", []):
            pid = post.get("id")
            if pid and pid not in items:
                items[pid] = {
                    "author": post.get("username", ""),
                    "text": (post.get("text") or "")[:300],
                    "timestamp": post.get("timestamp", ""),
                    "permalink": post.get("permalink", ""),
                    "via": label,
                }
    public = any(item["author"] != me for item in items.values())
    out = {
        "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "public": public,
        "items": list(items.values())[:120],
    }
    out_path = Config.ROOT / "queue" / "niche_scan.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=1, ensure_ascii=False),
                         encoding="utf-8")
    print(f"Scanned {len(items)} posts (public access: {public}) -> {out_path}")
    if not public:
        print("Only own posts visible: request Meta approval for "
              "threads_keyword_search (see README), then re-scan.")


def _cmd_post_smart(args):
    import datetime

    from .strategist import (
        SMART_MODELS,
        build_prompt,
        evaluate,
        finalize_thread,
        load_strategy,
        record_run,
        save_strategy,
        track_progress,
    )

    now = datetime.datetime.now(datetime.timezone.utc)

    if not args.dry_run and not args.force:
        from .guard import should_run

        run, reason = should_run(POSTED_LOG, max_per_day=args.max_per_day)
        if not run:
            print(f"SKIP: {reason}")
            return 0

    from .llm import chat as llm_chat

    client = ThreadsClient()
    posts = fetch_posts(client, max_posts=40, max_workers=20)
    scorecard = evaluate(posts, now=now)
    strategy = load_strategy()
    pivoted = scorecard["pivot"]
    from .insights import extract_account_totals

    try:
        totals = extract_account_totals(client.get_user_insights())
    except Exception:
        totals = {"views": None, "likes": None, "followers": None}
    progress = track_progress(strategy, totals)
    print(
        f"n={scorecard['sample_size']} median {scorecard['median_vph']}/h, "
        f"floor {scorecard['floor_vph']}/h, streak {scorecard['streak']}, "
        f"pivot={pivoted}, followers={progress['followers']}, "
        f"hypothesis: {strategy['hypothesis'][:80]}"
    )

    prompt = build_prompt(scorecard, strategy, progress)
    if args.dry_run:
        print("DRY-RUN: scorecard above, no LLM call, nothing published")
        return 0

    from .strategist import BRAIN_MODELS, decide_direction

    direction = None
    try:
        direction = decide_direction(
            llm_chat, BRAIN_MODELS, scorecard, strategy, progress)
        print(f"Brain model: {llm_module.last_model}")
        print(f"Brain direction: {direction['topic']} / "
              f"{direction['hook'][:100]}")
        if direction["hypothesis"]:
            strategy["hypothesis"] = direction["hypothesis"][:300]
        if not direction["post"]:
            print(f"Brain sits out this hour: {direction['post_reason']}")
            history = strategy.get("history", [])
            history.append({
                "run_at": now.isoformat(),
                "pillar": "smart-skip",
                "hook_sig": "",
                "texts": [],
                "model": llm_module.last_model,
                "hypothesis": strategy["hypothesis"][:300],
                "ids": [],
                "pivoted": False,
            })
            strategy["history"] = history[-50:]
            save_strategy(strategy)
            return 0
        prompt += (
            "\nStrategist direction, follow it exactly:\n"
            f"TOPIC: {direction['topic']}\n"
            f"HOOK: {direction['hook']}\n"
            f"CHANGE: {direction['change']}\n"
        )
    except Exception as exc:  # noqa: BLE001 - brain optional, writer covers
        print(f"Brain failed ({exc}); strategist writes directly")

    system = (
        "You run a Threads account posting software engineering content. "
        "Your only goal is views. Follow the output format exactly."
    )
    raw = llm_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": prompt}],
        max_tokens=1500,
        temperature=0.9,
        model=SMART_MODELS,
    )
    print(f"Strategist model: {llm_module.last_model}")
    taken_sigs = {hook_signature(p.get("text") or "") for p in posts}
    taken_sigs |= set(load_state().get("recent_hooks", []))

    def _attempt(raw_text):
        thread, hypothesis = finalize_thread(raw_text)
        if hook_signature(thread[0]) in taken_sigs:
            raise ValueError(
                f"hook too similar to a recent post: {thread[0][:60]}"
            )
        return thread, hypothesis

    try:
        thread, hypothesis = _attempt(raw)
    except (ValueError, RuntimeError) as exc:
        print(f"Strategist output rejected ({exc}); one retry with notes")
        raw = llm_chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": (
                 prompt + f"\nYour last output failed: {exc}. "
                 "Fix ONLY that and return the full thread again."
             )}],
            max_tokens=1500,
            temperature=0.7,
            model=SMART_MODELS,
        )
        try:
            thread, hypothesis = _attempt(raw)
        except (ValueError, RuntimeError) as exc2:
            print(f"LLM failed twice ({exc2}); bank fallback")
            thread = None

    if thread is None:
        pillar, thread = _pick_fresh_thread()
        hypothesis = strategy["hypothesis"]
        print(f"Bank fallback [{pillar}]")

    print(f"Hook: {thread[0].splitlines()[0][:120]}")

    try:
        ids = _publish_resilient(client, thread)
    except (ThreadsAPIError, requests.RequestException) as exc:
        print(f"Smart publish failed ({exc}); bank fallback")
        pillar, thread = _pick_fresh_thread()
        hypothesis = strategy["hypothesis"]
        print(f"Bank fallback [{pillar}]")
        ids = _publish_resilient(client, thread)

    run_at = now.isoformat()
    POSTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(POSTED_LOG, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"run_at": run_at, "pillar": "smart", "ids": ids})
            + "\n"
        )
    # Keep bank dedup aware of the smart hook without breaking rotation.
    bank_state = load_state()
    bank_state["recent_hooks"] = list(
        bank_state.get("recent_hooks", [])
    ) + [hook_signature(thread[0])]
    save_state(bank_state)

    if hypothesis:
        strategy["hypothesis"] = hypothesis[:300]
    record_run(strategy, "smart", thread, ids, run_at, pivoted,
               model=llm_module.last_model)
    save_strategy(strategy)
    print(f"Posted smart thread ({len(ids)}): {ids[0]}")

    try:
        from .replier import answer_pending

        done, skipped, _ = answer_pending(
            client, llm_chat, SMART_MODELS, dry_run=args.dry_run)
        print(f"Replies: {len(done)} posted, notes: {skipped[:3]}")
    except Exception as exc:  # noqa: BLE001 - replies never break posting
        print(f"Reply pass skipped ({exc})")


def _cmd_reply_comments(args):
    from .replier import answer_pending
    from .llm import chat as llm_chat
    from .strategist import SMART_MODELS

    client = ThreadsClient()
    done, skipped, _ = answer_pending(
        client, llm_chat, SMART_MODELS,
        max_replies=args.max, dry_run=args.dry_run)
    print(f"Replies posted: {len(done)}")
    for note in skipped[:10]:
        print(f"  - {note}")


def _account_brief(max_posts=100):
    """Compact data pack for the LLM: summary + top posts + themes."""
    client = ThreadsClient()
    posts = fetch_posts(client, max_posts=max_posts, max_workers=20)
    from .insights import summarise

    summary = summarise(posts) or {}
    ranked = sorted(
        [p for p in posts if (p.get("metrics") or {}).get("views") is not None],
        key=views_of,
        reverse=True,
    )[:15]
    lines = [
        f"Posts analysed: {summary.get('analysed', len(posts))}, "
        f"total views: {summary.get('total_views', 0):,}, "
        f"median: {summary.get('median_views', 0):,}, "
        f"top share: {summary.get('top_share', 0):.1f}%"
    ]
    for i, post in enumerate(ranked, start=1):
        text = (post.get("text") or "")[:200].replace("\n", " ")
        lines.append(
            f"#{i} views={views_of(post):,} eng={engagement_of(post)} "
            f"rate={engagement_rate(post) * 100:.1f}% "
            f"date={(post.get('timestamp') or '')[:10]} text={text}"
        )
    top_words = word_frequencies(ranked).most_common(12)
    lines.append("Top words: " + ", ".join(f"{w}({c})" for w, c in top_words))
    return "\n".join(lines)


def _cmd_analyze(args):
    brief = _account_brief(max_posts=args.max_posts)
    report = chat(
        [
            {
                "role": "system",
                "content": (
                    "You are a Threads growth analyst for a software "
                    "engineering account. Be concrete, cite the data, "
                    "no generic advice."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Here is my account performance data:\n\n" + brief + "\n\n"
                    "Report exactly these sections:\n"
                    "1. Top 3 view drivers (patterns with numbers)\n"
                    "2. Top 2 engagement drivers (what earns replies)\n"
                    "3. Next 3 post topics with a suggested hook line each\n"
                    "4. One thing to stop doing\n"
                    "Keep it under 400 words."
                ),
            },
        ],
        max_tokens=1200,
        temperature=0.4,
    )
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"\nSaved to {args.out}")


def _load_style_guide():
    path = Config.ROOT / "queue" / "STYLE.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "Hook ALL CAPS, no counters; replies sentence case; last post follows @owlsky17."


def _cmd_write_thread(args):
    pillar = args.pillar or "leetcode"
    examples = [
        t["posts"]
        for t in load_templates()
        if t["pillar"] == pillar
    ][:2]
    fewshot = ""
    for posts in examples:
        fewshot += "\n---EXAMPLE---\n" + "\n---\n".join(posts) + "\n"
    raw = chat(
        [
            {
                "role": "system",
                "content": (
                    "You write viral Threads posts for a software "
                    "engineering account. Follow the house style exactly."
                ),
            },
            {
                "role": "user",
                "content": (
                    "House style:\n" + _load_style_guide() + "\n"
                    + fewshot
                    + f"\nWrite ONE new 5-post thread about: {args.topic}\n"
                    "Pillar: " + pillar + "\n"
                    "Output ONLY the 5 posts, separated by a line "
                    "containing exactly --- and nothing else. "
                    "Every post under 500 characters. "
                    "Do NOT number the posts and do NOT write counters "
                    "like 1/5 anywhere. "
                    "Plain ASCII only: no em dashes, no en dashes, "
                    "commas and plain hyphens only. The last post is "
                    "always the follow CTA for @owlsky17."
                ),
            },
        ],
        max_tokens=1500,
        temperature=0.8,
    )
    posts = [
        chunk.strip()
        for chunk in raw.split("\n---\n")
        if chunk.strip()
    ]
    # Tolerate the model numbering the separators differently.
    posts = [p for p in posts if len(p) > 10][:5]
    problems = validate_thread(posts)
    if problems:
        print("STYLE CHECK FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        print("\n--- RAW MODEL OUTPUT ---\n" + raw)
        raise RuntimeError("AI output violates STYLE.md; nothing saved.")

    slug = "".join(
        c if c.isalnum() else "-" for c in args.topic.lower()
    ).strip("-")[:40] or "thread"
    draft_path = Config.ROOT / "queue" / "pending" / f"draft-ai-{slug}.json"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(
        json.dumps(
            {"topic": args.topic, "pillar": pillar, "style": "AI",
             "thread": posts},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Draft saved to {draft_path}")
    for i, post in enumerate(posts, start=1):
        print(f"\n[{i}] {post}")

    if args.publish:
        client = ThreadsClient()
        ids = client.publish_thread(posts, reply_wait_seconds=60)
        receipt = draft_path.with_suffix(".published.json")
        receipt.write_text(
            json.dumps({"texts": posts, "ids": ids}, indent=2,
                       ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nPublished: {ids}")


def _pick_fresh_thread(exclude_live=True):
    """Pick next bank thread, also skipping hooks already live recently.

    Guards against stale state.json (manual posts, uncommitted runs):
    the same hook text posted twice within minutes is rejected by
    Meta with code 24, so live-post signatures join the dedup set.
    """
    templates = load_templates()
    state = load_state()
    if exclude_live:
        try:
            client = ThreadsClient()
            page = client.list_posts(limit=10)
            live = {
                hook_signature((p.get("text") or ""))
                for p in page.get("data", [])
            }
            state["recent_hooks"] = list(
                state.get("recent_hooks", [])
            ) + sorted(live)
        except (ThreadsAPIError, requests.RequestException) as exc:
            print(f"Live-dedup check skipped ({exc})")
    template = choose_template(templates, state)
    problems = validate_thread(template["posts"])
    if problems:
        raise RuntimeError(
            f"Template from {template['source']} violates STYLE.md: "
            + "; ".join(problems)
        )
    return template["pillar"], list(template["posts"])


def _publish_resilient(client, thread):
    """Publish with Meta-flake tolerance. Returns post ids.

    Retries network errors 3x with backoff. On API errors (code 24
    during replication-lag windows, observed lasting over an hour)
    retries the SAME thread at 60s and 180s before giving up — caller
    falls back to a different thread only after all attempts fail.
    """
    import time

    backoff = 10
    for attempt in range(3):
        try:
            return client.publish_thread(thread, reply_wait_seconds=60)
        except requests.RequestException as exc:
            if attempt == 2:
                raise
            print(
                f"Attempt {attempt + 1} network error ({exc}); "
                f"retry in {backoff}s"
            )
            time.sleep(backoff)
            backoff *= 2
        except ThreadsAPIError as exc:
            waits = [60, 180]
            if attempt >= len(waits):
                raise
            wait = waits[attempt]
            print(
                f"Publish hit API error ({exc}); "
                f"retry same thread in {wait}s"
            )
            time.sleep(wait)
            try:
                return client.publish_thread(thread, reply_wait_seconds=60)
            except (ThreadsAPIError, requests.RequestException) as exc2:
                if attempt == len(waits) - 1:
                    raise
                print(f"Retry failed ({exc2}); continuing")


def _cmd_post_hourly(args):
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)

    if not args.dry_run and not args.force:
        from .guard import should_run

        run, reason = should_run(
            POSTED_LOG, max_per_day=args.max_per_day
        )
        if not run:
            print(f"SKIP: {reason}")
            return 0

    pillar, posts = _pick_fresh_thread()
    print(f"[{pillar}] {posts[0].splitlines()[0][:80]}")

    if args.dry_run:
        print(f"DRY-RUN OK: {len(posts)} posts, style valid")
        return 0

    client = ThreadsClient()
    try:
        ids = _publish_resilient(client, posts)
    except (ThreadsAPIError, requests.RequestException) as exc:
        # API rejected the content (e.g. code 24 duplicate) — retrying
        # identical text never helps, so switch to a fresh template.
        print(f"Content rejected ({exc}); picking a different thread")
        state = load_state()
        state["recent_hooks"] = list(
            state.get("recent_hooks", [])
        ) + [hook_signature(posts[0])]
        save_state(state)
        pillar, posts = _pick_fresh_thread(exclude_live=False)
        print(f"[{pillar}] {posts[0].splitlines()[0][:80]}")
        ids = _publish_resilient(client, posts)

    run_at = now.isoformat()
    POSTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(POSTED_LOG, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"run_at": run_at, "pillar": pillar, "ids": ids}) + "\n"
        )
    mark_used(pillar, posts, run_at=run_at)
    print(f"Posted {len(ids)}: {ids[0]}")


def _cmd_publish(args):
    client = ThreadsClient()
    wait = 0 if args.no_wait else 30

    if args.carousel:
        result = client.publish_carousel(args.text, args.carousel, wait_seconds=wait)
    elif args.image_url:
        result = client.publish_image(args.text, args.image_url, wait_seconds=wait)
    elif args.video_url:
        result = client.publish_video(args.text, args.video_url, wait_seconds=wait)
    else:
        result = client.publish_text(args.text)

    print(f"Post published: {result.get('id', 'unknown')}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="threads-publish",
        description="Publish posts to Meta Threads via the official API",
    )
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="Log in and save an access token")
    auth.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser",
    )
    auth.add_argument(
        "--redirect-uri",
        help="Override OAUTH_REDIRECT_URI for this run (e.g. an https tunnel)",
    )
    auth.set_defaults(func=_cmd_auth)

    verify = sub.add_parser("verify", help="Check the stored credentials work")
    verify.set_defaults(func=_cmd_verify)

    doctor = sub.add_parser("doctor", help="Diagnose connection problems")
    doctor.set_defaults(func=_cmd_doctor)

    top = sub.add_parser(
        "top-posts",
        help="Rank posts by views (needs threads_manage_insights)",
    )
    top.add_argument("--limit", type=int, default=10, help="How many posts to show")
    top.add_argument(
        "--max-posts",
        type=int,
        default=200,
        help="Stop after fetching this many posts",
    )
    top.add_argument(
        "--json",
        dest="json_out",
        help="Also write the raw post data to this file",
    )
    top.add_argument(
        "--progress",
        action="store_true",
        help="Show per-post progress on stderr",
    )
    top.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Concurrent insights requests (default 20, use 1 for sequential)",
    )
    top.set_defaults(func=_cmd_top_posts)

    pub = sub.add_parser("publish", help="Publish a post to Threads")
    pub.add_argument("--text", required=True, help="Post text (max 500 characters)")
    pub.add_argument("--image-url", help="Public URL of a single image")
    pub.add_argument("--video-url", help="Public URL of a single video")
    pub.add_argument("--carousel", nargs="+", help="Public URLs of 2-20 images")
    pub.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip the 30s media-processing wait before publishing",
    )
    pub.set_defaults(func=_cmd_publish)

    delete = sub.add_parser("delete", help="Delete your own post (needs threads_delete)")
    delete.add_argument("--post-id", required=True, help="Media ID from publish or list")
    delete.set_defaults(func=_cmd_delete)

    scan = sub.add_parser(
        "scan-niche",
        help="TOP-search pillars + top authors into queue/niche_scan.json",
    )
    scan.add_argument(
        "--authors",
        help="Comma-separated author usernames (default: built-in list)",
    )
    scan.add_argument(
        "--limit", type=int, default=10, help="Posts per query (default 10)"
    )
    scan.set_defaults(func=_cmd_scan_niche)

    analyze = sub.add_parser(
        "analyze", help="AI analysis of your top posts (uses OpenRouter)"
    )
    analyze.add_argument(
        "--max-posts", type=int, default=100, help="Posts to fetch (default 100)"
    )
    analyze.add_argument("--out", help="Also save the report to this file")
    analyze.set_defaults(func=_cmd_analyze)

    write = sub.add_parser(
        "write-thread", help="AI-draft a thread in house style (uses OpenRouter)"
    )
    write.add_argument("--topic", required=True, help="Thread topic")
    write.add_argument(
        "--pillar",
        default="leetcode",
        help="Style pillar for examples (default leetcode)",
    )
    write.add_argument(
        "--publish",
        action="store_true",
        help="Publish after validation (default: draft only)",
    )
    write.set_defaults(func=_cmd_write_thread)

    hourly = sub.add_parser(
        "post-hourly", help="Publish next bank thread (for cron/Actions)"
    )
    hourly.add_argument(
        "--dry-run",
        action="store_true",
        help="Pick and validate a thread without publishing",
    )
    hourly.add_argument(
        "--max-per-day",
        type=int,
        default=24,
        help="Daily post cap (default 24)",
    )
    hourly.add_argument(
        "--force",
        action="store_true",
        help="Bypass time-since-last and daily-cap guards (testing)",
    )
    hourly.set_defaults(func=_cmd_post_hourly)

    smart = sub.add_parser(
        "post-smart", help="Autonomous LLM loop: measure, decide, publish, pivot"
    )
    smart.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate + show scorecard without LLM call or publish",
    )
    smart.add_argument(
        "--force",
        action="store_true",
        help="Bypass time-since-last and daily-cap guards (testing)",
    )
    smart.add_argument(
        "--max-per-day",
        type=int,
        default=24,
        help="Daily post cap (default 24)",
    )
    smart.set_defaults(func=_cmd_post_smart)

    reply = sub.add_parser(
        "reply-comments", help="Answer pending comments (uses OpenRouter)"
    )
    reply.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be answered without publishing",
    )
    reply.add_argument(
        "--max", type=int, default=5, help="Max replies per run (default 5)"
    )
    reply.set_defaults(func=_cmd_reply_comments)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    try:
        outcome = args.func(args)
    except (
        ThreadsAPIError,
        LLMError,
        EnvironmentError,
        ValueError,
        RuntimeError,
        requests.RequestException,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return outcome if isinstance(outcome, int) else 0


if __name__ == "__main__":
    sys.exit(main())
