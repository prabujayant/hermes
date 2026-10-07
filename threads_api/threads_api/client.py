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
        return self.publish_container(container["id"])

    def publish_thread(self, texts, reply_wait_seconds=30):
        """Publish a hook + chained replies. Returns list of post ids.

        Uses reply_to_id chaining: post[0] is published first, each next
        post replies to the previous published post id. Replies need ~30s
        server-side processing between container creation and publishing
        (same as image/video media), otherwise threads_publish returns
        code 24 "resource does not exist".
        """
        if not texts:
            raise ValueError("Thread needs at least 1 post")
        ids = []
        reply_to = None
        for text in texts:
            container = self.create_text_container(text, reply_to_id=reply_to)
            if reply_to is not None and reply_wait_seconds > 0:
                time.sleep(reply_wait_seconds)
            result = self.publish_container(container["id"])
            post_id = result.get("id")
            ids.append(post_id)
            reply_to = post_id
        return ids

    def publish_replies(self, texts, root_post_id, reply_wait_seconds=30):
        """Publish replies chained under an existing post. Returns post ids."""
        if not texts:
            raise ValueError("Need at least 1 reply")
        ids = []
        reply_to = root_post_id
        for text in texts:
            container = self.create_text_container(text, reply_to_id=reply_to)
            if reply_wait_seconds > 0:
                time.sleep(reply_wait_seconds)
            result = self.publish_container(container["id"])
            post_id = result.get("id")
            ids.append(post_id)
            reply_to = post_id
        return ids

    def publish_image(self, text, image_url, wait_seconds=30):
        container = self.create_image_container(text, image_url)
        return self._publish_after_wait(container["id"], wait_seconds)

    def publish_video(self, text, video_url, wait_seconds=30):
        container = self.create_video_container(text, video_url)
        return self._publish_after_wait(container["id"], wait_seconds)

    def publish_carousel(self, text, image_urls, wait_seconds=30):
        container = self.create_carousel_container(text, image_urls)
        return self._publish_after_wait(container["id"], wait_seconds)

    def _publish_after_wait(self, creation_id, wait_seconds):
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        return self.publish_container(creation_id)
