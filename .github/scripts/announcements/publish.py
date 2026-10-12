"""Validate, preview, and deliver announcements without automatic write retries."""

import base64
import html
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


CHANNELS = {"linkedin", "bluesky", "mastodon", "slack", "groupsio", "reddit"}
BUFFER_CHANNELS = {"linkedin", "bluesky", "mastodon"}
DRAFT_CHANNELS = BUFFER_CHANNELS | {"groupsio"}
MODES = {"dry-run", "draft", "publish"}
REPORT_FIELDS = ("channel", "target", "action", "status", "remote_id", "remote_status", "error")
SECRETS = {
    "linkedin": ("BUFFER_API_KEY",),
    "bluesky": ("BUFFER_API_KEY",),
    "mastodon": ("BUFFER_API_KEY",),
    "slack": ("SLACK_WEBHOOK_URL",),
    "groupsio": ("GROUPSIO_API_KEY",),
    "reddit": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_REFRESH_TOKEN"),
}


class DeliveryError(Exception):
    """An explicitly reported validation or delivery failure."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials or turn an API write into a redirected GET.
        return None


def request(url, payload, headers=None, *, form=False, empty_ok=False, raw=False):
    data = urlencode(payload).encode() if form else json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
        **(headers or {}),
    }
    try:
        with build_opener(NoRedirects()).open(
            Request(url, data=data, headers=headers, method="POST"), timeout=30
        ) as response:
            body = response.read()
    except HTTPError as exc:
        detail = ""
        if raw:
            # Slack webhooks report errors as short plain-text codes.
            try:
                detail = f" ({api_error_code(exc.read().decode().strip())})"
            except (OSError, UnicodeDecodeError, AttributeError):
                pass
        raise DeliveryError(f"API returned HTTP {exc.code}{detail}; not retried") from exc
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise DeliveryError(
            "API connection failed; delivery may have occurred; not retried"
        ) from exc
    if raw:
        return body.decode("utf-8", errors="replace")
    if not body and empty_ok:
        return None
    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeliveryError("API returned an invalid JSON response; not retried") from exc
    if not isinstance(result, dict):
        raise DeliveryError("API returned an unexpected response object")
    return result


def text_field(value, name, *, limit=40000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DeliveryError(f"{name} must be a nonempty string of at most {limit} characters")
    if any(
        (ord(char) < 32 and char not in "\n\t") or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise DeliveryError(f"{name} contains control characters")
    return value


def slack_escape(text):
    # Slack treats <...> as control sequences such as <!channel>, even with parse=none.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_json(value, name):
    try:
        return json.loads(value)
    except (ValueError, TypeError) as exc:
        raise DeliveryError(f"{name} must be valid JSON") from exc


def api_error_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,80}", value) else "unknown"


def build_plan(announcement, channels, targets, mode):
    if mode not in MODES:
        raise DeliveryError("mode must be dry-run, draft, or publish")
    if not isinstance(announcement, dict) or set(announcement) - {
        "title", "text", "url", "overrides"
    }:
        raise DeliveryError("announcement must contain only title, text, url, and overrides")
    title = text_field(announcement.get("title"), "title", limit=300)
    text = text_field(announcement.get("text"), "text")
    url = text_field(announcement.get("url"), "url", limit=2048)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise DeliveryError("url must be a valid absolute HTTPS URL") from exc
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username
        or parsed.password or any(char.isspace() for char in url)
    ):
        raise DeliveryError("url must be an absolute HTTPS URL without credentials or whitespace")
    if (
        not isinstance(channels, list) or not channels
        or any(not isinstance(channel, str) or channel not in CHANNELS for channel in channels)
        or len(set(channels)) != len(channels)
    ):
        raise DeliveryError("channels must be a nonempty JSON array of unique supported channels")
    if not isinstance(targets, dict) or set(targets) - CHANNELS:
        raise DeliveryError("targets must be an object keyed by supported channel names")
    overrides = announcement.get("overrides", {})
    if not isinstance(overrides, dict) or set(overrides) - CHANNELS:
        raise DeliveryError("overrides must be an object keyed by supported channel names")
    for channel, override in overrides.items():
        if not isinstance(override, dict) or set(override) - {"title", "text"}:
            raise DeliveryError(f"{channel} override may contain only title and text")
        for key, value in override.items():
            text_field(value, f"{channel}.{key}", limit=300 if key == "title" else 40000)
    # Validate configured targets even when not selected, to catch configuration typos.
    for channel, destinations in targets.items():
        if not isinstance(destinations, list) or not destinations:
            raise DeliveryError(f"{channel} targets must be a nonempty array")
        for destination in destinations:
            if channel == "groupsio":
                valid = type(destination) is int and destination > 0
            else:
                pattern = {
                    "slack": r"[CG][A-Z0-9]+|#[a-z0-9_-]{1,80}",
                    "reddit": r"[A-Za-z0-9_]{2,21}",
                }.get(channel, r"[A-Za-z0-9_-]+")
                valid = isinstance(destination, str) and re.fullmatch(pattern, destination)
            if not valid:
                raise DeliveryError(f"Invalid {channel} destination")
        normalized = [str(value).lower() for value in destinations]
        if len(set(normalized)) != len(normalized):
            raise DeliveryError(f"Duplicate {channel} destination")
    plan = []
    buffer_ids = set()
    for channel in channels:
        if channel not in targets:
            raise DeliveryError(f"No targets configured for {channel}")
        override = overrides.get(channel, {})
        channel_title = override.get("title", title)
        body = f"{override.get('text', text)}\n\n{url}"
        if channel == "slack" and len(slack_escape(body)) > 40000:
            raise DeliveryError("Slack content exceeds 40000 characters")
        for destination in targets[channel]:
            if channel in BUFFER_CHANNELS:
                if destination in buffer_ids:
                    raise DeliveryError("A Buffer channel ID cannot target multiple services")
                buffer_ids.add(destination)
            action = (
                "preview" if mode == "dry-run" or (mode == "draft" and channel not in DRAFT_CHANNELS)
                else mode
            )
            plan.append({
                "channel": channel, "target": destination, "action": action,
                "title": channel_title, "text": body, "url": url,
                "status": "pending",
            })
    if len(plan) > 20:
        raise DeliveryError("At most 20 destinations may be selected per run")
    return plan


class Publisher:
    def __init__(self, credentials, *, groupsio_host="lists.cncf.io", reddit_user_agent=""):
        if groupsio_host not in {"lists.cncf.io", "groups.io"}:
            raise DeliveryError("groupsio_host must be lists.cncf.io or groups.io")
        self.credentials = credentials
        self.groupsio_base = f"https://{groupsio_host}/api/v1"
        self.reddit_user_agent = reddit_user_agent
        self.reddit_token = None

    def auth(self, secret):
        return {"Authorization": f"Bearer {self.credentials[secret]}"}

    def buffer(self, query, variables):
        result = request(
            "https://api.buffer.com", {"query": query, "variables": variables},
            self.auth("BUFFER_API_KEY"),
        )
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise DeliveryError("Buffer GraphQL request failed")
        return result["data"]

    def preflight(self, plan):
        active = [item for item in plan if item["action"] != "preview"]
        missing = sorted({
            secret for item in active for secret in SECRETS[item["channel"]]
            if not self.credentials.get(secret)
        })
        if missing:
            raise DeliveryError(f"Missing secrets: {', '.join(missing)}")
        required = {secret for item in active for secret in SECRETS[item["channel"]]}
        for secret in required:
            value = self.credentials[secret]
            if not isinstance(value, str) or any(not 33 <= ord(char) <= 126 for char in value):
                raise DeliveryError(f"{secret} must contain only printable ASCII without whitespace")
        if any(item["channel"] == "slack" for item in active) and not re.fullmatch(
            r"https://hooks\.slack\.com/[A-Za-z0-9/_-]+", self.credentials["SLACK_WEBHOOK_URL"]
        ):
            raise DeliveryError("SLACK_WEBHOOK_URL must be a https://hooks.slack.com/ URL")
        if any(item["channel"] == "reddit" for item in active):
            text_field(self.reddit_user_agent, "reddit_user_agent", limit=256)
            if any(not 32 <= ord(char) <= 126 for char in self.reddit_user_agent):
                raise DeliveryError("reddit_user_agent must be a single line of printable ASCII")
        # Check every Buffer channel before the first write: a mislabeled ID must
        # not silently route a LinkedIn announcement to a different network.
        for item in active:
            if item["channel"] not in BUFFER_CHANNELS:
                continue
            data = self.buffer(
                "query($id: ChannelId!) { channel(input: {id: $id}) "
                "{ id service isDisconnected isLocked } }",
                {"id": item["target"]},
            )
            channel = data.get("channel")
            if (
                not isinstance(channel, dict)
                or channel.get("id") != item["target"]
                or channel.get("service") != item["channel"]
                or channel.get("isDisconnected") is not False
                or channel.get("isLocked") is not False
            ):
                raise DeliveryError("Buffer channel is unavailable or does not match its selected service")

    def deliver(self, item):
        channel = item["channel"]
        if channel in BUFFER_CHANNELS:
            data = self.buffer(
                "mutation($input: CreatePostInput!) { createPost(input: $input) "
                "{ ... on PostActionSuccess { post { id status } } "
                "... on MutationError { message } } }",
                {"input": {
                    "channelId": item["target"], "text": item["text"],
                    "schedulingType": "automatic",
                    "mode": "addToQueue" if item["action"] == "draft" else "shareNow",
                    "saveToDraft": item["action"] == "draft",
                }},
            )
            result = data.get("createPost")
            if not isinstance(result, dict) or result.get("message"):
                raise DeliveryError("Buffer rejected the post; check content limits and account permissions")
            post = result.get("post")
            if not isinstance(post, dict) or not post.get("id"):
                raise DeliveryError("Buffer did not return a post ID")
            item["remote_id"] = post["id"]
            item["remote_status"] = post.get("status")
            allowed = {"draft"} if item["action"] == "draft" else {"sending", "sent", "scheduled"}
            if post.get("status") not in allowed:
                raise DeliveryError("Buffer returned an unexpected post status")
        elif channel == "slack":
            result = request(
                self.credentials["SLACK_WEBHOOK_URL"],
                {"channel": item["target"], "text": slack_escape(item["text"]),
                 "mrkdwn": False, "parse": "none", "link_names": False, "unfurl_links": True},
                raw=True,
            )
            # Webhooks return a plain "ok" and no message ID.
            if result.strip() != "ok":
                raise DeliveryError(
                    f"Slack rejected the message ({api_error_code(result.strip())}); "
                    "check that the webhook can post to the channel"
                )
        elif channel == "groupsio":
            headers = self.auth("GROUPSIO_API_KEY")
            draft = request(
                f"{self.groupsio_base}/newdraft",
                {"group_id": item["target"], "draft_type": "draft_type_post"},
                headers, form=True,
            )
            if draft.get("object") != "draft" or type(draft.get("id")) is not int:
                raise DeliveryError("Groups.io did not return a draft ID")
            if draft["id"] <= 0:
                raise DeliveryError("Groups.io returned an invalid draft ID")
            item["remote_id"] = draft["id"]
            item["remote_status"] = "empty-draft"
            updated = request(
                f"{self.groupsio_base}/updatedraft",
                {"draft_id": draft["id"], "subject": item["title"],
                 "body": "<p>" + html.escape(item["text"]).replace("\n", "<br>\n") + "</p>"},
                headers, form=True,
            )
            if updated.get("object") != "draft" or updated.get("id") != draft["id"]:
                raise DeliveryError("Groups.io did not confirm the draft update")
            item["remote_status"] = "draft"
            if item["action"] == "publish":
                result = request(
                    f"{self.groupsio_base}/postdraft", {"draft_id": draft["id"]},
                    headers, form=True, empty_ok=True,
                )
                if result is not None:
                    raise DeliveryError("Groups.io returned an unexpected postdraft response")
                item["remote_status"] = "submitted"
        elif channel == "reddit":
            if self.reddit_token is None:
                basic = base64.b64encode(
                    f"{self.credentials['REDDIT_CLIENT_ID']}:{self.credentials['REDDIT_CLIENT_SECRET']}".encode()
                ).decode()
                token = request(
                    "https://www.reddit.com/api/v1/access_token",
                    {"grant_type": "refresh_token", "refresh_token": self.credentials["REDDIT_REFRESH_TOKEN"]},
                    {"Authorization": f"Basic {basic}", "User-Agent": self.reddit_user_agent},
                    form=True,
                )
                if not isinstance(token.get("access_token"), str) or not token["access_token"]:
                    raise DeliveryError("Reddit did not return an access token")
                self.reddit_token = token["access_token"]
            result = request(
                "https://oauth.reddit.com/api/submit",
                {"api_type": "json", "sr": item["target"], "kind": "link",
                 "title": item["title"], "url": item["url"],
                 "resubmit": "false", "sendreplies": "false"},
                {"Authorization": f"Bearer {self.reddit_token}", "User-Agent": self.reddit_user_agent},
                form=True,
            )
            result = result.get("json")
            if not isinstance(result, dict) or result.get("errors") != []:
                errors = result.get("errors", []) if isinstance(result, dict) else []
                codes = [
                    api_error_code(error[0]) for error in errors
                    if isinstance(error, list) and error
                ] if isinstance(errors, list) else []
                raise DeliveryError(
                    f"Reddit rejected the submission ({', '.join(codes) or 'unknown'}); "
                    "check API access, subreddit rules, and duplicates"
                )
            data = result.get("data")
            if not isinstance(data, dict) or not data.get("name"):
                raise DeliveryError("Reddit did not return a submission ID")
            item["remote_id"] = data["name"]
        else:
            raise DeliveryError("Unsupported delivery channel")

    def run(self, plan, on_progress=None):
        self.preflight(plan)
        for item in plan:
            if item["action"] == "preview":
                item["status"] = "preview"
                if on_progress:
                    on_progress()
                continue
            try:
                self.deliver(item)
            except DeliveryError as exc:
                item["status"] = "failed"
                item["error"] = str(exc)
            else:
                item["status"] = "drafted" if item["action"] == "draft" else "submitted"
            if on_progress:
                on_progress()
        return not any(item["status"] == "failed" for item in plan)


def main():
    output = Path(os.environ["ANNOUNCEMENT_REPORT_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    report = {"mode": os.environ.get("ANNOUNCEMENT_MODE", "dry-run"), "deliveries": []}
    success = False

    def save_report():
        # The report is an audit record; rendered content is reproducible from the inputs.
        saved = {**report, "deliveries": [
            {key: value for key, value in item.items() if key in REPORT_FIELDS}
            for item in report["deliveries"]
        ]}
        temporary = output / "report.json.tmp"
        temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "report.json")

    try:
        report["deliveries"] = build_plan(
            parse_json(os.environ.get("ANNOUNCEMENT", ""), "announcement"),
            parse_json(os.environ.get("ANNOUNCEMENT_CHANNELS", ""), "channels"),
            parse_json(os.environ.get("ANNOUNCEMENT_TARGETS", ""), "targets"),
            report["mode"],
        )
        publisher = Publisher(
            os.environ, groupsio_host=os.environ.get("GROUPSIO_HOST", "lists.cncf.io"),
            reddit_user_agent=os.environ.get("REDDIT_USER_AGENT", ""),
        )
        save_report()
        success = publisher.run(report["deliveries"], on_progress=save_report)
    except DeliveryError as exc:
        report["error"] = str(exc)
    report["success"] = success
    save_report()
    summary = ["## Announcement delivery", "", f"Mode: `{report['mode']}`", ""]
    for status in ("preview", "drafted", "submitted", "failed", "pending"):
        count = sum(item["status"] == status for item in report["deliveries"])
        summary.append(f"- {status}: {count}")
    if "error" in report:
        summary.extend(["", report["error"]])
    rendered = {}
    for item in report["deliveries"]:
        rendered.setdefault(item["channel"], item)
    if rendered:
        summary.extend(["", "### Rendered content", ""])
    for channel, item in rendered.items():
        content = item["text"] if channel != "reddit" else item["url"]
        summary.append(
            f"<details><summary>{channel}: {html.escape(item['title'])}</summary>"
            f"<pre>{html.escape(content)}</pre></details>"
        )
    summary.extend(["", "See the announcement report artifact for destinations and delivery IDs."])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n".join(summary) + "\n")
    print("Announcement delivery completed." if success else "Announcement delivery failed; inspect report.json.", file=sys.stderr)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
