"""Stdlib-only schedule guard for the auto-poster worker.

Single source of truth for the skip policy, shared by cli.py and the
GitHub workflow (which runs this *before* pip install so no-op hours
cost ~20s of runner time instead of ~2min).

Exit codes: 0 = run, 2 = skip.
"""

import datetime
import json
import sys
from pathlib import Path

MIN_GAP_MINUTES = 25
MAX_PER_DAY = 40


def should_run(posted_log, now=None,
               min_gap_minutes=MIN_GAP_MINUTES, max_per_day=MAX_PER_DAY):
    """Return (run: bool, reason: str)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    last = None
    count = 0
    try:
        with open(posted_log, encoding="utf-8") as handle:
            for line in handle:
                try:
                    receipt = json.loads(line)
                except ValueError:
                    continue
                stamp = receipt.get("run_at")
                if not stamp:
                    continue
                if stamp[:10] == today:
                    count += 1
                try:
                    moment = datetime.datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                if last is None or moment > last:
                    last = moment
    except OSError:
        return True, "no receipt log yet"
    if last and (now - last).total_seconds() < min_gap_minutes * 60:
        return False, f"last post {last.isoformat()} (<{min_gap_minutes} min ago)"
    if count >= max_per_day:
        return False, f"daily cap {max_per_day} reached"
    return True, "due"


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    posted_log = Path(argv[0]) if argv else Path("queue/posted.jsonl")
    run, reason = should_run(posted_log)
    print(("RUN: " if run else "SKIP: ") + reason)
    return 0 if run else 2


if __name__ == "__main__":
    sys.exit(main())
