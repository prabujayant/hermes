"""Reply engine: answer new comments on own threads, fast and on-voice.

Why: replies (especially replies-to-replies inside the first hours)
are the heaviest Threads ranking signal. An unanswered comment is a
dead conversation; an answered one re-enters ranking.

Safety rules (non-negotiable):
- Never reply to our own comments, never reply twice to one comment.
- Replies are short (<=280 chars), plain ASCII, no counters, no CTA,
  never argumentative. One sharp follow-up question is encouraged
  (sparks reply-to-reply).
- Batched into ONE LLM call per run; capped replies per run.
- Failures never break the calling worker (best-effort tail step).
"""

import datetime
import json
from pathlib import Path

from .config import Config
from .strategist import ascii_clean

STATE_PATH = Config.ROOT / "queue" / "replies_state.json"
MAX_REPLIES_PER_RUN = 5
COMMENT_WINDOW_HOURS = 48
REPLY_MAX_CHARS = 280


def load_reply_state(path=None):
    try:
        state = json.loads(Path(path or STATE_PATH).read_text(
            encoding="utf-8"))
    except (ValueError, OSError):
        state = {}
    state.setdefault("answered", [])
    return state


def save_reply_state(state, path=None):
    path = Path(path or STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _age_hours(stamp, now):
    try:
        born = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return 0.0
    return max(0.0, (now - born).total_seconds() / 3600)


def collect_pending(client, own_username, max_threads=10,
                    window_hours=COMMENT_WINDOW_HOURS, now=None):
    """Recent unanswered comments on own threads, oldest first.

    Returns (pending, state, threads_scanned). A scan that errors still
    returns what it found, so callers can tell empty from broken.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = load_reply_state()
    answered = set(state.get("answered", []))
    pending = []
    scanned = 0
    try:
        page = client.list_posts(limit=max_threads)
    except Exception:
        return [], state, scanned
    for post in page.get("data", []):
        scanned += 1
        try:
            conv = client._get(post["id"] + "/conversation",
                               {"fields": "id,text,username,timestamp",
                                "limit": 50})
        except Exception:
            continue
        for comment in conv.get("data", []):
            cid = comment.get("id", "")
            if not cid or cid in answered:
                continue
            if (comment.get("username", "") or "").lower() == \
                    (own_username or "").lower():
                continue
            if _age_hours(comment.get("timestamp", ""), now) > window_hours:
                continue
            pending.append({
                "id": cid,
                "author": comment.get("username", "?"),
                "text": (comment.get("text") or "")[:300],
                "timestamp": comment.get("timestamp", ""),
                "root": post["id"],
            })
    pending.sort(key=lambda c: c["id"])
    return pending, state, scanned


def build_reply_prompt(comments):
    lines = [
        "You manage replies for a software engineering Threads account. "
        "Voice: direct, concrete, friendly, zero fluff. NEVER argue, "
        "never lecture, never be sarcastic.",
        "For EACH comment below, write a reply of 1-2 short sentences "
        "(max 280 chars). Answer their question if they asked one, "
        "otherwise acknowledge with one concrete nugget. End with a "
        "short follow-up question when natural (sparks conversation).",
        "Rules: plain ASCII only (no em dashes), no counters, no follow "
        "CTA, no hashtags, no links. Output format per comment, exactly:",
        "[<comment id>] on its own line, then your reply lines.",
        "Separate comments with a line containing exactly ---.",
        "Comments:",
    ]
    for comment in comments:
        lines.append(
            f"[{comment['id']}] @{comment['author']}: {comment['text']}"
        )
    return "\n".join(lines)


def parse_replies(raw, wanted_ids):
    """Map comment id -> reply text. Raises ValueError on garbage."""
    wanted = set(wanted_ids)
    found = {}
    for chunk in raw.split("\n---\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        first, _, rest = chunk.partition("\n")
        cid = first.strip().strip("[]")
        if cid in wanted and rest.strip():
            found[cid] = rest.strip()
    if not found:
        raise ValueError("no usable replies parsed")
    return found


def already_answered_live(client, own_username, comment):
    """True when one of our posts in the thread is newer than the comment.

    Catches overlapping runs: run A published a reply seconds ago while
    run B holds a stale answered-list. Conservative by design (may skip
    a legit second reply in a busy thread). API errors fail open.
    """
    try:
        conv = client._get(comment["root"] + "/conversation",
                           {"fields": "username,timestamp", "limit": 50})
    except Exception:
        return False
    try:
        born = datetime.datetime.strptime(
            comment.get("timestamp", ""), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return False
    for post in conv.get("data", []):
        if (post.get("username", "") or "").lower() != \
                (own_username or "").lower():
            continue
        try:
            moment = datetime.datetime.strptime(
                post.get("timestamp", ""), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if moment > born:
            return True
    return False


def validate_reply(text):
    """Return problems list (empty = sendable)."""
    problems = []
    if not text or not text.strip():
        problems.append("empty reply")
        return problems
    if len(text) > REPLY_MAX_CHARS:
        problems.append(f"{len(text)} chars, max {REPLY_MAX_CHARS}")
    if ascii_clean(text) != text:
        problems.append("non-ASCII dashes present")
    low = text.lower()
    if "follow @" in low:
        problems.append("no CTA in replies")
    return problems


def answer_pending(client, llm_chat, models, max_replies=MAX_REPLIES_PER_RUN,
                   dry_run=False):
    """Full pass: collect, draft (1 LLM call), validate, publish.

    Returns (published, skipped, state). Never raises.
    """
    from .strategist import ascii_clean as clean

    published, skipped = [], []
    try:
        me = client.get_profile().get("username", "")
    except Exception as exc:
        return published, [f"profile failed: {exc}"], load_reply_state()
    pending, state, scanned = collect_pending(client, me)
    batch = pending[:max_replies]
    if not batch:
        return published, [f"nothing pending (scanned {scanned} threads)"], state
    if dry_run:
        return published, [f"DRY-RUN would answer {len(batch)} "
                           f"(scanned {scanned} threads)"] + batch, state
    try:
        raw = llm_chat(
            [{"role": "system",
              "content": "You write short helpful replies for a Threads "
                         "engineering account."},
             {"role": "user", "content": build_reply_prompt(batch)}],
            max_tokens=1200,
            temperature=0.7,
            model=models,
        )
        drafts = parse_replies(raw, [c["id"] for c in batch])
    except Exception as exc:
        return published, [f"drafting failed: {exc}"], state
    answered = set(state.get("answered", []))
    for comment in batch:
        text = clean(drafts.get(comment["id"], ""))
        problems = validate_reply(text)
        if problems:
            skipped.append(f"{comment['id']}: {'; '.join(problems)}")
            continue
        if already_answered_live(client, me, comment):
            skipped.append(f"{comment['id']}: answered live already")
            answered.add(comment["id"])
            continue
        try:
            container = client.create_text_container(
                text, reply_to_id=comment["id"])
            client.wait_for_container(container["id"])
            result = client.publish_container(container["id"])
            published.append(result.get("id"))
            answered.add(comment["id"])
        except Exception as exc:
            skipped.append(f"{comment['id']}: publish failed ({exc})")
    state["answered"] = sorted(answered)[-500:]
    save_reply_state(state)
    return published, skipped, state
