import hashlib
import hmac
import time

import requests

from .config import Config

TEXT_MAX_LENGTH = 500
CAROUSEL_MIN_ITEMS = 2
CAROUSEL_MAX_ITEMS = 20

INSIGHT_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")


class ThreadsAPIError(Exception):
    """Raised when the Threads API returns an error payload."""


class ThreadsClient:
    """Minimal client for the Threads Graph API (graph.threads.net)."""

    def __init__(self, session=None):
        Config.validate()
        self.access_token = Config.ACCESS_TOKEN
        self.user_id = Config.THREADS_USER_ID
        self.base_url = Config.BASE_URL
        self.session = session or requests.Session()

    def _auth_fields(self):
        fields = {"access_token": self.access_token}
        if Config.APP_SECRET:
            fields["appsecret_proof"] = hmac.new(
                Config.APP_SECRET.encode("utf-8"),
                self.access_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return fields

    @staticmethod
    def _handle(response):
        try:
            payload = response.json()
        except ValueError:
            raise ThreadsAPIError(
                f"Threads returned a non-JSON response (HTTP {response.status_code})"
            ) from None

        error = payload.get("error")
        if error:
            message = error.get("message", "Unknown Threads API error")
            code = error.get("code")
            if code:
                message = f"{message} (code {code})"
            raise ThreadsAPIError(message)

        if response.status_code >= 400:
            raise ThreadsAPIError(f"Threads API returned HTTP {response.status_code}")

        return payload

    def _post(self, endpoint, data):
        url = f"{self.base_url}/{self.user_id}/{endpoint}"
        response = self.session.post(url, data={**data, **self._auth_fields()})
        return self._handle(response)

    def _get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"
        response = self.session.get(
            url, params={**(params or {}), **self._auth_fields()}
        )
        return self._handle(response)

    def get_profile(self):
        """Return the authenticated account; used to verify the connection."""
        return self._get(
            "me", {"fields": "id,username,name,threads_profile_picture_url"}
        )

    def list_posts(self, limit=50, after=None):
        """List the account's own posts, newest first."""
        params = {
            "fields": "id,text,permalink,timestamp,media_type,username",
            "limit": limit,
        }
        if after:
            params["after"] = after
        return self._get(f"{self.user_id}/threads", params)

    def get_post_insights(self, post_id, metrics=INSIGHT_METRICS):
        """Insights for a single post. Requires threads_manage_insights."""
        return self._get(f"{post_id}/insights", {"metric": ",".join(metrics)})

    def _delete(self, media_id):
        url = f"{self.base_url}/{media_id}"
        response = self.session.delete(url, params=self._auth_fields())
        return self._handle(response)

    def delete_post(self, media_id):
        """Delete own post. Requires threads_delete scope (100/day)."""
        return self._delete(media_id)

    def keyword_search(self, query, search_type="TOP", author_username=None,
                       media_type="TEXT", limit=25):
        """Search public Threads posts. Needs threads_keyword_search scope
        AND Meta app approval for that permission; without approval the
        API only returns the authenticated user's own posts. Note: no
        view counts are available for other users' posts — TOP ranking
        is Meta's popularity proxy."""
        params = {"q": query, "search_type": search_type,
                  "media_type": media_type, "limit": limit,
                  "fields": "id,text,username,timestamp,permalink"}
        if author_username:
            params["author_username"] = author_username.lstrip("@")
        return self._get("keyword_search", params)

    def get_container_status(self, container_id):
        """Poll media-container readiness: PENDING/IN_PROGRESS/FINISHED/ERROR."""
        payload = self._get(container_id, {"fields": "status"})
        return (payload.get("status") or "UNKNOWN").upper()

    def wait_for_container(self, container_id, timeout=90, interval=5):
        """Block until the container is FINISHED (read-your-write).

        Publishing the instant a container is created races server-side
        replication and fails with code 24, especially from datacenter
        networks. Polling status guarantees the publish will find it.
        """
        deadline = time.monotonic() + timeout
        last = "UNKNOWN"
        while True:
            try:
                last = self.get_container_status(container_id)
            except ThreadsAPIError:
                last = "UNKNOWN"
            if last == "FINISHED":
                return last
            if last == "ERROR":
                raise ThreadsAPIError(
                    f"Media container {container_id} failed processing"
                )
            if time.monotonic() >= deadline:
                raise ThreadsAPIError(
                    f"Timed out waiting for container {container_id} "
                    f"(last status {last})"
                )
            time.sleep(interval)

    def get_user_insights(self, metrics=("views", "likes", "followers_count")):
        """Account-level insights. Requires threads_manage_insights."""
        return self._get(
            f"{self.user_id}/threads_insights", {"metric": ",".join(metrics)}
        )

    @staticmethod
    def _validate_text(text):
        if text and len(text) > TEXT_MAX_LENGTH:
            raise ValueError(
                f"Text is {len(text)} characters; Threads allows {TEXT_MAX_LENGTH}"
            )

    def create_text_container(self, text, reply_to_id=None):
        self._validate_text(text)
        data = {"media_type": "TEXT", "text": text}
        if reply_to_id:
            data["reply_to_id"] = reply_to_id
        return self._post("threads", data)

    def create_image_container(self, text, image_url):
        self._validate_text(text)
        return self._post(
            "threads",
            {"media_type": "IMAGE", "text": text, "image_url": image_url},
        )

    def create_video_container(self, text, video_url):
        self._validate_text(text)
        return self._post(
            "threads",
            {"media_type": "VIDEO", "text": text, "video_url": video_url},
        )

    def create_carousel_container(self, text, image_urls):
        self._validate_text(text)
        if not CAROUSEL_MIN_ITEMS <= len(image_urls) <= CAROUSEL_MAX_ITEMS:
            raise ValueError(
                "Carousel requires between "
                f"{CAROUSEL_MIN_ITEMS} and {CAROUSEL_MAX_ITEMS} items "
                f"({len(image_urls)} given)"
            )
        return self._post(
            "threads",
            {
                "media_type": "CAROUSEL",
                "text": text,
                "carousel_media": ",".join(image_urls),
            },
        )

    def publish_container(self, creation_id):
        return self._post("threads_publish", {"creation_id": creation_id})

    def publish_text(self, text, reply_to_id=None):
        container = self.create_text_container(text, reply_to_id=reply_to_id)
        self.wait_for_container(container["id"])
        return self.publish_container(container["id"])

    def publish_thread(self, texts, reply_wait_seconds=60):
        """Publish a hook + chained replies. Returns list of post ids.

        Uses reply_to_id chaining: post[0] is published first, each next
        post replies to the previous published post id. Every container
        is polled until FINISHED before publishing — instant publishes
        race server-side replication and fail with code 24.
        """
        if not texts:
            raise ValueError("Thread needs at least 1 post")
        ids = []
        reply_to = None
        for text in texts:
            container = self.create_text_container(text, reply_to_id=reply_to)
            self.wait_for_container(container["id"], timeout=reply_wait_seconds)
            result = self.publish_container(container["id"])
            post_id = result.get("id")
            ids.append(post_id)
            reply_to = post_id
        return ids

    def publish_replies(self, texts, root_post_id, reply_wait_seconds=60):
        """Publish replies chained under an existing post. Returns post ids."""
        if not texts:
            raise ValueError("Need at least 1 reply")
        ids = []
        reply_to = root_post_id
        for text in texts:
            container = self.create_text_container(text, reply_to_id=reply_to)
            self.wait_for_container(container["id"], timeout=reply_wait_seconds)
            result = self.publish_container(container["id"])
            post_id = result.get("id")
            ids.append(post_id)
            reply_to = post_id
        return ids

    def publish_image(self, text, image_url, wait_seconds=90):
        container = self.create_image_container(text, image_url)
        return self._publish_after_wait(container["id"], wait_seconds)

    def publish_video(self, text, video_url, wait_seconds=90):
        container = self.create_video_container(text, video_url)
        return self._publish_after_wait(container["id"], wait_seconds)

    def publish_carousel(self, text, image_urls, wait_seconds=90):
        container = self.create_carousel_container(text, image_urls)
        return self._publish_after_wait(container["id"], wait_seconds)

    def _publish_after_wait(self, creation_id, wait_seconds):
        self.wait_for_container(creation_id, timeout=wait_seconds or 90)
        return self.publish_container(creation_id)
