import json

import pytest

from threads_api.client import ThreadsAPIError
from threads_api.insights import (
    engagement_of,
    engagement_rate,
    extract_metrics,
    fetch_posts,
    rank_by_views,
    render_report,
    summarise,
    views_of,
    word_frequencies,
)


def make_post(post_id, text, views=None, likes=0, timestamp="2026-01-01T12:00:00+0000"):
    post = {
        "id": post_id,
        "text": text,
        "timestamp": timestamp,
        "media_type": "TEXT",
        "permalink": f"https://threads.com/p/{post_id}",
    }
    if views is not None:
        post["metrics"] = {"views": views, "likes": likes}
    return post


class TestExtractMetrics:
    def test_flattens_insight_payload(self):
        payload = {
            "data": [
                {"name": "views", "values": [{"value": 120}]},
                {"name": "likes", "values": [{"value": 7}]},
            ]
        }
        assert extract_metrics(payload) == {"views": 120, "likes": 7}

    def test_ignores_empty_and_non_numeric(self):
        payload = {
            "data": [
                {"name": "views", "values": []},
                {"name": "likes", "values": [{"value": "n/a"}]},
                {"values": [{"value": 5}]},
            ]
        }
        assert extract_metrics(payload) == {}

    def test_handles_missing_data_key(self):
        assert extract_metrics({}) == {}


class TestMetricHelpers:
    def test_views_and_engagement(self):
        post = make_post("1", "hello", views=100, likes=10)
        post["metrics"].update({"replies": 5, "reposts": 2, "quotes": 1, "shares": 3})

        assert views_of(post) == 100
        assert engagement_of(post) == 21

    def test_engagement_rate_guards_against_zero_views(self):
        assert engagement_rate(make_post("1", "x", views=0, likes=5)) == 0.0
        assert engagement_rate(make_post("1", "x")) == 0.0

    def test_missing_metrics_are_zero(self):
        assert views_of({"id": "1"}) == 0
        assert engagement_of({"id": "1"}) == 0


class TestRanking:
    def test_orders_by_views_descending(self):
        posts = [
            make_post("a", "low", views=10),
            make_post("b", "high", views=900),
            make_post("c", "mid", views=100),
        ]
        ranked = rank_by_views(posts, top_n=2)

        assert [p["id"] for p in ranked] == ["b", "c"]

    def test_excludes_posts_without_view_data(self):
        posts = [make_post("a", "measured", views=5), make_post("b", "unmeasured")]
        assert [p["id"] for p in rank_by_views(posts)] == ["a"]


class TestSummarise:
    def test_computes_overview_stats(self):
        posts = [
            make_post("a", "x", views=100, likes=10),
            make_post("b", "y", views=300, likes=30),
            make_post("c", "z"),
        ]
        summary = summarise(posts)

        assert summary["analysed"] == 3
        assert summary["measured"] == 2
        assert summary["unmeasured"] == 1
        assert summary["total_views"] == 400
        assert summary["mean_views"] == 200
        assert summary["median_views"] == 200
        assert summary["max_views"] == 300
        assert summary["total_engagement"] == 40
        assert summary["top_share"] == pytest.approx(75.0)

    def test_returns_none_without_view_data(self):
        assert summarise([make_post("a", "x")]) is None


class TestWordFrequencies:
    def test_drops_stopwords_and_short_words(self):
        posts = [make_post("a", "The quick leetcode and the leetcode grind")]
        counts = word_frequencies(posts)

        assert counts["leetcode"] == 2
        assert "the" not in counts
        assert "and" not in counts


class FakeClient:
    def __init__(self, pages, insights, fail_on=None):
        self.pages = pages
        self.insights = insights
        self.fail_on = fail_on or set()
        self.calls = []

    def list_posts(self, limit=50, after=None):
        index = 0 if after is None else int(after)
        page = self.pages[index]
        self.calls.append(("list_posts", index))
        nxt = index + 1
        if nxt < len(self.pages):
            return {**page, "paging": {"cursors": {"after": str(nxt)}}}
        return {**page, "paging": {"cursors": {}}}

    def get_post_insights(self, post_id, metrics=None):
        self.calls.append(("insights", post_id))
        if post_id in self.fail_on:
            raise ThreadsAPIError("no permission")
        return {"data": [{"name": "views", "values": [{"value": self.insights[post_id]}]}]}


class TestFetchPosts:
    def test_paginates_and_attaches_metrics(self):
        pages = [
            {"data": [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]},
            {"data": [{"id": "3", "text": "c"}]},
        ]
        client = FakeClient(pages, {"1": 10, "2": 20, "3": 30})

        posts = fetch_posts(client, pause=0)

        assert [p["id"] for p in posts] == ["1", "2", "3"]
        assert [views_of(p) for p in posts] == [10, 20, 30]

    def test_records_insight_failures_without_aborting(self):
        pages = [{"data": [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]}]
        client = FakeClient(pages, {"1": 10, "2": 20}, fail_on={"1"})

        posts = fetch_posts(client, pause=0)

        assert posts[0]["metrics"] == {}
        assert "no permission" in posts[0]["metrics_error"]
        assert views_of(posts[1]) == 20

    def test_respects_max_posts(self):
        pages = [{"data": [{"id": str(i), "text": "x"} for i in range(5)]}]
        client = FakeClient(pages, {str(i): i for i in range(5)})

        posts = fetch_posts(client, max_posts=3, pause=0)
        assert len(posts) == 3

    def test_reports_progress(self):
        pages = [{"data": [{"id": "1", "text": "a"}]}]
        client = FakeClient(pages, {"1": 5})
        seen = []

        fetch_posts(client, pause=0, on_progress=lambda d, t, p: seen.append((d, t)))
        assert seen == [(1, 1)]


class TestRenderReport:
    def test_includes_top_posts_and_overview(self):
        posts = [
            make_post("a", "Leetcode grind tips", views=1000, likes=50),
            make_post("b", "Small update", views=20, likes=1),
        ]
        report = render_report(posts, top_n=2, username="owlsky17")

        assert "@owlsky17" in report
        assert "Top 2 posts by views" in report
        assert "1,000" in report
        assert "Leetcode grind tips" in report
        assert "Total views" in report

    def test_explains_missing_permission(self):
        report = render_report([make_post("a", "no metrics")])

        assert "No view data" in report
        assert "threads_manage_insights" in report

    def test_truncates_long_text(self):
        long_text = "word " * 100
        report = render_report([make_post("a", long_text, views=1)], top_n=1)

        assert "\u2026" in report
        # The full 500-character body must not be rendered anywhere.
        assert max(len(line) for line in report.splitlines()) < 120
        assert report.count("word") < 40
