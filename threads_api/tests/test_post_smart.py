import datetime

from threads_api import cli as cli_module


def _post(text, views, hours_ago, pid="p1"):
    born = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=hours_ago)
    return {
        "id": pid,
        "text": text,
        "timestamp": born.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "metrics": {"views": views, "likes": 1, "replies": 0,
                    "reposts": 0, "quotes": 0, "shares": 0},
    }


class _Args:
    dry_run = False
    force = True
    max_per_day = 40


def _run_post_smart(monkeypatch, tmp_path, brain_raw, thread_raw):
    import json

    posts = [_post("old winner here", 500, 7, pid="old1"),
             _post("old flop here", 5, 6, pid="old2"),
             _post("old mid here", 100, 5, pid="old3")]

    class FakeClient:
        def list_posts(self, limit=50, after=None):
            return {"data": posts}

        def get_post_insights(self, pid):
            return {"data": []}

        def get_user_insights(self):
            return {"data": []}

        def get_profile(self):
            return {"username": "tester"}

        def _get(self, endpoint, params):
            return {"data": []}

        def publish_thread(self, thread, reply_wait_seconds=60):
            return ["live1", "live2"]

    calls = {"n": 0}

    def fake_chat(messages, max_tokens=1500, temperature=0.7, model=None):
        calls["n"] += 1
        return brain_raw if calls["n"] == 1 else thread_raw

    saved = {}

    def fake_load_strategy():
        return {"hypothesis": "old idea", "history": []}

    def fake_save_strategy(state, path=None):
        saved["state"] = json.loads(json.dumps(state))

    monkeypatch.setattr(cli_module, "ThreadsClient", lambda: FakeClient())
    monkeypatch.setattr("threads_api.llm.chat", fake_chat)
    monkeypatch.setattr("threads_api.strategist.load_strategy", fake_load_strategy)
    monkeypatch.setattr("threads_api.strategist.save_strategy", fake_save_strategy)
    monkeypatch.setattr(cli_module, "load_state", lambda: {"recent_hooks": []})
    monkeypatch.setattr(cli_module, "save_state", lambda s: None)
    monkeypatch.setattr(cli_module, "POSTED_LOG",
                        tmp_path / "posted.jsonl")
    monkeypatch.setattr(cli_module.llm_module, "last_model", "test-model")
    assert cli_module._cmd_post_smart(_Args()) is None
    return saved["state"]


def test_brain_hypothesis_survives_writer_echo(monkeypatch, tmp_path):
    brain = ("POST: YES go\nTOPIC: backend caching\n"
             "HOOK: debate\nCHANGE: none\nHYPOTHESIS: brain leads here")
    thread = ("HOOK TEXT HERE\n---\nvalue here\n---\nthird here\n---\n"
              "fourth here\n---\nFollow @owlsky17 for more\n"
              "HYPOTHESIS: writer echo here")
    state = _run_post_smart(monkeypatch, tmp_path, brain, thread)
    assert state["hypothesis"] == "brain leads here"
    assert state["history"][-1]["model"] == "test-model"


def test_writer_echo_leads_without_brain(monkeypatch, tmp_path):
    def bad_brain(messages, max_tokens=400, temperature=0.7, model=None):
        raise ValueError("brain down")

    import threads_api.llm as llm_module

    posts = [_post("old winner here", 500, 7, pid="old1"),
             _post("old flop here", 5, 6, pid="old3")]
    saved = {}
    import json

    class FakeClient:
        def list_posts(self, limit=50, after=None):
            return {"data": posts}

        def get_post_insights(self, pid):
            return {"data": []}

        def get_user_insights(self):
            return {"data": []}

        def get_profile(self):
            return {"username": "tester"}

        def _get(self, endpoint, params):
            return {"data": []}

        def publish_thread(self, thread, reply_wait_seconds=60):
            return ["live1"]

    calls = {"n": 0}

    def fake_chat(messages, max_tokens=1500, temperature=0.7, model=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("brain down")
        return ("SOLO HOOK\n---\nvalue\n---\nFollow @owlsky17 x\n"
                "HYPOTHESIS: solo writer leads")

    monkeypatch.setattr(cli_module, "ThreadsClient", lambda: FakeClient())
    monkeypatch.setattr("threads_api.llm.chat", fake_chat)
    monkeypatch.setattr(
        "threads_api.strategist.load_strategy",
        lambda: {"hypothesis": "old idea", "history": []})
    monkeypatch.setattr(
        "threads_api.strategist.save_strategy",
        lambda s, *a: saved.update(state=json.loads(json.dumps(s))))
    monkeypatch.setattr(cli_module, "load_state", lambda: {"recent_hooks": []})
    monkeypatch.setattr(cli_module, "save_state", lambda s: None)
    monkeypatch.setattr(cli_module, "POSTED_LOG", tmp_path / "posted.jsonl")
    monkeypatch.setattr(llm_module, "last_model", "test-model")
    assert cli_module._cmd_post_smart(_Args()) is None
    assert saved["state"]["hypothesis"] == "solo writer leads"
