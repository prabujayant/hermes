# Threads API Publisher

CLI tool for publishing to Meta Threads through the official Threads Graph API
(`graph.threads.net`).

Supports text, single image, single video, and carousel posts (2–20 items).

## 1. Set up the Meta app

The Threads API only accepts apps that have the **Threads** use case added.

1. Open the [Meta App Dashboard](https://developers.facebook.com/apps) and create
   (or open) an app.
2. **Add the Threads use case**: *Use cases* → *Add* → **Threads**.
   The app must have a **Threads profile** linked, which requires an Instagram
   account.
3. Go to *App settings* → *Basic* and copy the **Threads App ID**.
   This is **not** always the same as the Facebook App ID — use the Threads one.
4. Under *Threads* → *Settings*, add a valid OAuth redirect URI. It **must be
   https** — Meta blocks http redirects with "Insecure login blocked". Use
   `https://localhost:8080/callback`; it must match `OAUTH_REDIRECT_URI` exactly.
5. While testing, add your own account under *App roles* → *Roles* → **Threads
   Tester** so you can publish without going through App Review.

## 2. Configure credentials

```bash
cp .env.example .env
```

Fill in `.env`:

| Variable | Description |
| --- | --- |
| `APP_ID` | **Threads App ID** (not the Facebook App ID) |
| `APP_SECRET` | Threads app secret |
| `THREADS_USER_ID` | Your Threads user ID (filled in by `auth`) |
| `ACCESS_TOKEN` | Filled in automatically by `auth` |
| `OAUTH_REDIRECT_URI` | Must match an entry in your app's redirect URIs |

`.env` is gitignored — never commit real credentials.

## 3. Usage

```bash
pip install -r requirements.txt

# Diagnose configuration problems first
python -m threads_api.cli doctor

# Log in (opens a browser, then writes the token into .env)
python -m threads_api.cli auth

# Confirm the connection
python -m threads_api.cli verify

# Publish
python -m threads_api.cli publish --text "Hello from the Threads API!"
python -m threads_api.cli publish --text "Look" --image-url https://example.com/a.jpg
python -m threads_api.cli publish --text "Look" --video-url https://example.com/a.mp4
python -m threads_api.cli publish --text "Set" --carousel https://a.jpg https://b.jpg
```

Useful flags:

- `--no-wait` — skip the 30 s media-processing wait before publishing.
- `auth --no-browser` — print the authorization URL instead of opening a browser.

## How publishing works

Threads uses a two-step flow:

1. **Create a container** — `POST /{user-id}/threads` with the content.
   Returns a `creation_id`.
2. **Publish it** — `POST /{user-id}/threads_publish` with the `creation_id`.

Media is processed server-side, so the tool waits ~30 s between the steps for
image, video, and carousel posts.

Authentication uses the Threads OAuth 2.0 code flow. The short-lived token
(~1 hour) is immediately exchanged for a long-lived token (~60 days).

## API limits

| Limit | Value |
| --- | --- |
| Published posts | 250 per profile per 24 hours |
| Text length | 500 characters |
| Carousel items | 2–20 |
| Editing | Not possible after publishing |
| Scheduling | Not supported — the API publishes immediately |
| Media | Must be a publicly reachable URL; no direct uploads |

Because the API cannot schedule, build a queue plus a cron/scheduler that
invokes `publish` at the desired times.

## Troubleshooting

Run `python -m threads_api.cli doctor` to check the whole chain at once.

| Symptom | Cause and fix |
| --- | --- |
| `Invalid client_id` | The app has no Threads use case, or `APP_ID` is the Facebook App ID. Add the Threads use case and use the **Threads App ID**. |
| `Invalid redirect_uri` | `OAUTH_REDIRECT_URI` is not listed in the app's valid OAuth redirect URIs, or it does not match exactly (watch trailing slashes). |
| `Invalid OAuth access token` | The token expired (long-lived tokens last ~60 days) or was revoked. Run `auth` again. |
| `Insufficient permissions` | The token lacks `threads_content_publish`. Re-run `auth` and approve both scopes. |
| Redirect page shows an error | The redirect URI is not registered, or the account is not a Threads Tester on the app. |
| `Insecure login blocked` (1349187) | `OAUTH_REDIRECT_URI` is `http://`. Meta requires https; use `https://localhost:8080/callback`. |
| Browser warns about the certificate | Expected — the local callback uses a self-signed certificate. Choose *Advanced* → *Proceed*. |
| Port already in use | Something else is on 8080; change `OAUTH_REDIRECT_URI` and the app setting together. |

## Hourly automation

`post-hourly` publishes the next thread from the template bank
(`queue/bank/*.json`, style locked in `queue/STYLE.md`):

```bash
python -m threads_api.cli post-hourly --dry-run   # pick + validate only
python -m threads_api.cli post-hourly             # publish (5 posts, ~2.5 min)
```

Guardrails: skips if the last post is <50 min old, caps at 24/day,
rotates pillars, never repeats a hook from the last 30 posts, retries
API errors 3x with backoff. State lives in `queue/state.json` and
`queue/posted.jsonl` (both committed).

GitHub Actions (`.github/workflows/threads-hourly.yml`, hourly at :07 UTC)
needs these repo secrets: `APP_ID`, `APP_SECRET`, `ACCESS_TOKEN`,
`THREADS_USER_ID` (copy from your `.env` after `auth`).

Long-lived tokens expire after ~60 days: the workflow runs `verify`
first and fails loud so you notice. Re-run `auth` locally and update
the secrets when that happens.

## Development

```bash
python -m pytest tests/ -q
```

Layout:

- `threads_api/config.py` — environment loading and validation
- `threads_api/client.py` — Graph API client (containers, publishing, errors)
- `threads_api/auth.py` — OAuth flow and callback server
- `threads_api/diagnose.py` — pre-flight checks behind `doctor`
- `threads_api/cli.py` — command-line interface
