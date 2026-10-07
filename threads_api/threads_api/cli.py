import argparse
import json
import sys

import requests

from .auth import save_credentials, start_oauth_flow
from .client import ThreadsAPIError, ThreadsClient
from .config import Config
from .diagnose import run_checks
from .generator import POSTED_LOG, mark_used, next_thread
from .insights import fetch_posts, render_report


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


def _recent_posts_today(path):
    """Count posted.jsonl receipts from today (UTC)."""
    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    count = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    if json.loads(line).get("run_at", "")[:10] == today:
                        count += 1
                except ValueError:
                    continue
    except OSError:
        pass
    return count


def _last_run_at(path):
    """UTC datetime of the most recent receipt, or None."""
    import datetime

    last = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    stamp = json.loads(line).get("run_at")
                    if stamp:
                        last = datetime.datetime.fromisoformat(stamp)
                except ValueError:
                    continue
    except OSError:
        pass
    return last


def _cmd_post_hourly(args):
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)

    if not args.dry_run:
        last = _last_run_at(POSTED_LOG)
        if last and (now - last).total_seconds() < 50 * 60:
            print(f"SKIP: last post {last.isoformat()} (<50 min ago)")
            return 0
        if _recent_posts_today(POSTED_LOG) >= args.max_per_day:
            print(f"SKIP: daily cap {args.max_per_day} reached")
            return 0

    pillar, posts = next_thread()
    print(f"[{pillar}] {posts[0].splitlines()[0][:80]}")

    if args.dry_run:
        print(f"DRY-RUN OK: {len(posts)} posts, style valid")
        return 0

    client = ThreadsClient()
    backoff = 10
    for attempt in range(3):
        try:
            ids = client.publish_thread(posts, reply_wait_seconds=30)
            break
        except (ThreadsAPIError, requests.RequestException) as exc:
            if attempt == 2:
                raise
            print(f"Attempt {attempt + 1} failed ({exc}); retry in {backoff}s")
            import time

            time.sleep(backoff)
            backoff *= 2

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
    hourly.set_defaults(func=_cmd_post_hourly)

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
