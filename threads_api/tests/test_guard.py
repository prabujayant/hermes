import datetime
import json

from threads_api.guard import MAX_PER_DAY, MIN_GAP_MINUTES, main, should_run


def write_log(path, stamps):
    with open(path, "w", encoding="utf-8") as handle:
        for stamp in stamps:
            handle.write(json.dumps({"run_at": stamp, "pillar": "x"}) + "\n")


def test_no_log_means_run(tmp_path):
    run, _ = should_run(tmp_path / "missing.jsonl")
    assert run is True


def test_recent_post_means_skip(tmp_path):
    log = tmp_path / "posted.jsonl"
    now = datetime.datetime.now(datetime.timezone.utc)
    write_log(log, [(now - datetime.timedelta(minutes=10)).isoformat()])
    run, reason = should_run(log)
    assert run is False
    assert "min ago" in reason


def test_old_post_means_run(tmp_path):
    log = tmp_path / "posted.jsonl"
    now = datetime.datetime.now(datetime.timezone.utc)
    write_log(log, [(now - datetime.timedelta(hours=3)).isoformat()])
    run, _ = should_run(log)
    assert run is True


def test_daily_cap_means_skip(tmp_path):
    log = tmp_path / "posted.jsonl"
    noon = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    write_log(
        log,
        [
            (noon - datetime.timedelta(minutes=60 + i)).isoformat()
            for i in range(MAX_PER_DAY)
        ],
    )
    run, reason = should_run(log, now=noon)
    assert run is False
    assert "cap" in reason


def test_pause_file_means_skip_until_removed(tmp_path):
    log = tmp_path / "posted.jsonl"
    pause = tmp_path / "PAUSED"
    pause.write_text("automation paused\n", encoding="utf-8")
    run, reason = should_run(log)
    assert run is False
    assert "paused" in reason
    assert "automation paused" in reason
    assert main([str(log)]) == 2

    pause.unlink()
    run, _ = should_run(log)
    assert run is True


def test_bad_lines_ignored(tmp_path):
    log = tmp_path / "posted.jsonl"
    log.write_text("not json\n{}\n", encoding="utf-8")
    run, _ = should_run(log)
    assert run is True


def test_main_exit_codes(tmp_path):
    log = tmp_path / "posted.jsonl"
    assert main([str(tmp_path / "missing.jsonl")]) == 0
    now = datetime.datetime.now(datetime.timezone.utc)
    write_log(log, [now.isoformat()])
    assert main([str(log)]) == 2
    assert MIN_GAP_MINUTES == 25
