# Announcements

Reusable workflow for distributing OpenTelemetry news and blog announcements.
The caller supplies prepared content, chooses services, and maps each service to
one or more destinations. LinkedIn, Bluesky, and Mastodon are delivered through
Buffer; Slack, CNCF mailing lists (Groups.io), and Reddit use their APIs directly.

## Modes

| Mode | Buffer | Groups.io | Slack and Reddit |
| ---- | ------ | --------- | ---------------- |
| `dry-run` (default) | Local preview | Local preview | Local preview |
| `draft` | Create native drafts | Create native drafts | Local preview, **no sending** |
| `publish` | Request immediate publishing | Post draft to the list | Send message / submit link |

Dry runs make **no API requests** and need no credentials. They validate local
configuration and render the intended content, but cannot check account access,
remote IDs, platform-specific character limits, or service availability.
Draft runs require credentials only for Buffer and Groups.io destinations.
Draft creation is a remote write; repeating it creates additional drafts.
Slack and Reddit previews are not native drafts and cannot later be published
from their services' interfaces.

## Calling the workflow

```yaml
name: Announce a blog post
on:
  workflow_dispatch:
    inputs:
      mode:
        type: choice
        options: [dry-run, draft, publish]
        default: dry-run

permissions: {}

jobs:
  announce:
    uses: open-telemetry/shared-workflows/.github/workflows/announcements.yml@<commit-sha>
    with:
      announcement: >-
        {"title":"New OpenTelemetry blog post",
         "text":"Learn about the latest OpenTelemetry improvements.",
         "url":"https://opentelemetry.io/blog/2026/example/"}
      channels: '["linkedin","bluesky","mastodon","slack","groupsio","reddit"]'
      targets: ${{ vars.ANNOUNCEMENT_TARGETS }}
      mode: ${{ inputs.mode }}
      environment: announcements
      reddit_user_agent: "github-actions:otel-announcements:1.0 (by /u/YOUR_BOT)"
    secrets:
      BUFFER_API_KEY: ${{ secrets.BUFFER_API_KEY }}
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
      GROUPSIO_API_KEY: ${{ secrets.GROUPSIO_API_KEY }}
      REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}
      REDDIT_CLIENT_SECRET: ${{ secrets.REDDIT_CLIENT_SECRET }}
      REDDIT_REFRESH_TOKEN: ${{ secrets.REDDIT_REFRESH_TOKEN }}
```

Replace `<commit-sha>` with a reviewed shared-workflows commit. The self-repository
action (`$/...`) loads delivery code from that exact workflow commit; no consumer
checkout or floating code ref is used by the reusable workflow.

Configure an `announcements` environment in the **calling repository**, with
required reviewers and deployment branch restrictions before enabling remote
writes. Environment secrets can supply these credentials instead of passing
repository secrets explicitly; same-named environment secrets take precedence.
Do not call this workflow from untrusted PR code or `pull_request_target` jobs
that check out a PR head. Only announce reviewed content from a trusted branch,
and restrict which workflows/repositories can access the publishing credentials.

### Inputs

| Input | Required / default | Meaning |
| ----- | ------------------ | ------- |
| `announcement` | Required | JSON object described below |
| `channels` | Required | Nonempty JSON array of unique service names |
| `targets` | Required | JSON object mapping service names to destination arrays |
| `mode` | `dry-run` | `dry-run`, `draft`, or `publish` |
| `environment` | `announcements` | Caller environment for approvals and environment secrets |
| `groupsio_host` | `lists.cncf.io` | Either `lists.cncf.io` or `groups.io`; arbitrary hosts are rejected |
| `reddit_user_agent` | Empty | Required for Reddit publishing; descriptive user agent identifying the app and bot account |
| `report_name` | `announcements` | Unique artifact label for multiple calls/matrix entries in the same workflow run |

The supported names are `linkedin`, `bluesky`, `mastodon`, `slack`, `groupsio`,
and `reddit` (note the spelling **mastodon**).

Example `ANNOUNCEMENT_TARGETS` repository variable:

```json
{
  "linkedin": ["BUFFER_LINKEDIN_CHANNEL_ID"],
  "bluesky": ["BUFFER_BLUESKY_CHANNEL_ID"],
  "mastodon": ["BUFFER_MASTODON_CHANNEL_ID"],
  "slack": ["C0123456789", "C9876543210"],
  "groupsio": [12345, 67890],
  "reddit": ["opentelemetry"]
}
```

These IDs are illustrative, not the organization's actual destinations.
Buffer destinations are connected account/channel IDs, **not** organization IDs;
use a separate ID for each service. Slack destinations are channel IDs, not
names or webhook URLs. Groups.io destinations are positive integer group IDs
on the selected host, not email addresses. Reddit destinations are subreddit
names without `/r/`. There is no implicit destination, even for Reddit: specify
`opentelemetry` explicitly. Each selected service must have targets. Unselected
services are not contacted. At most 20 destinations may be selected per run.

### Announcement content / file format

Store announcements as UTF-8 JSON files, for example
`.announcements/collector-release.json` in `opentelemetry.io`:

```json
{
  "title": "OpenTelemetry Collector release",
  "text": "A new Collector release is available. Read about the improvements.",
  "url": "https://opentelemetry.io/blog/2026/collector-release/",
  "overrides": {
    "bluesky": {
      "text": "New Collector release! Here is what changed."
    },
    "groupsio": {
      "title": "Collector release announcement",
      "text": "Hello everyone,\n\nA new Collector release is available."
    },
    "reddit": {
      "title": "What is new in the OpenTelemetry Collector?"
    }
  }
}
```

`title`, `text`, and `url` are required; unknown fields are rejected.
Titles must be nonempty and at most 300 characters; text must be nonempty and at
most 40,000 characters. URLs must be absolute HTTPS URLs without embedded
credentials or whitespace. `overrides` is optional and may replace `title`
and/or `text` for each service. Keep destination selection in the caller's
configuration rather than in content files.

Buffer, Slack, and Groups.io receive `text`, a blank line, then the URL.
Groups.io also uses `title` as the subject and receives escaped HTML, not raw
HTML from the file. Slack text is sent without markup/mention parsing.
Reddit receives a **link post** with `title` and `url`; it does not use `text`.
Provide short social copy, not the entire blog body. The APIs enforce network
limits; this workflow does not truncate content or create threads automatically.
Dry runs cannot guarantee that a platform will accept the content.

### Credentials and service setup

| Secret | Required for | Setup |
| ------ | ------------ | ----- |
| `BUFFER_API_KEY` | Buffer drafts/publishing | Generate a Buffer API key with access to the connected OpenTelemetry accounts |
| `SLACK_BOT_TOKEN` | Slack publishing | Install a bot with `chat:write` and invite it to each destination channel, including private channels |
| `GROUPSIO_API_KEY` | Groups.io drafts/publishing | API key for an account subscribed to the target lists, with `can_post` permission |
| `REDDIT_CLIENT_ID` | Reddit publishing | Approved Reddit OAuth application |
| `REDDIT_CLIENT_SECRET` | Reddit publishing | Application client secret |
| `REDDIT_REFRESH_TOKEN` | Reddit publishing | Bot account's refresh token with the `submit` scope |

Use the Groups.io account/API key associated with `lists.cncf.io` and obtain
group IDs on that host. Verify CNCF API access and membership before rollout.
The workflow uses `/newdraft`, `/updatedraft`, and, for publishing only,
`/postdraft`. List moderation still applies; acceptance does not guarantee
immediate email delivery. Review Groups.io drafts in the authenticated account.
If a draft update fails, the report retains its ID so it can be inspected or
removed manually.

For Reddit, obtain API approval and coordinate with subreddit moderators.
Access is not guaranteed merely by creating client credentials. Tokens are
refreshed at run time; password-based login is not used. Automatic duplicate
URL resubmission is disabled. Subreddit-required flair or other special posting
requirements are not supported by this initial link-post adapter.

## First consumer: opentelemetry.io

The integration belongs in `open-telemetry/opentelemetry.io`; adding this shared
workflow does **not** install or enable it there.

For blogs, invoke the reusable workflow from a job **after confirmed production
publication**, passing the public canonical URL and curated summary as
`announcement`. Do not interpret a merged PR, a future-dated Hugo file, or a
publish-readiness label as proof that the blog is live. The site's blog-label
workflow marks readiness, not deployment. For scheduled posts, the website
must detect the first actual publication and invoke the announcement once.
That publication detection and any persistent deduplication belong to the
consumer's publication pipeline.

For standalone announcements, this example reacts only to **new** files on
`main` and permits manually selecting a file for testing. Automatic runs use
`draft`; manual runs default to `dry-run`. Set `ANNOUNCEMENT_CHANNELS` to a JSON
array and `ANNOUNCEMENT_TARGETS` to the destination object above. A matrix entry
is created for each new file, with a unique report name.

```yaml
name: Announce new files
on:
  push:
    branches: [main]
    paths: [".announcements/*.json"]
  workflow_dispatch:
    inputs:
      file:
        description: "Announcement JSON path under .announcements/"
        required: true
        type: string
      channels:
        description: "JSON array of services (empty uses ANNOUNCEMENT_CHANNELS)"
        type: string
        default: ""
      mode:
        type: choice
        options: [dry-run, draft, publish]
        default: dry-run

permissions:
  contents: read

jobs:
  prepare:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    outputs:
      entries: ${{ steps.files.outputs.entries }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false
          ref: ${{ github.sha }}
      - id: files
        env:
          EVENT_NAME: ${{ github.event_name }}
          INPUT_FILE: ${{ inputs.file }}
          BEFORE: ${{ github.event.before }}
        run: |
          python3 - <<'PY'
          import json
          import os
          from pathlib import Path
          import subprocess

          root = Path(".announcements").resolve()
          if os.environ["EVENT_NAME"] == "workflow_dispatch":
              paths = [os.environ["INPUT_FILE"]]
          elif set(os.environ["BEFORE"]) == {"0"}:
              paths = sorted(str(p) for p in Path(".announcements").glob("*.json"))
          else:
              result = subprocess.check_output([
                  "git", "diff", "--name-only", "--diff-filter=A", "-z",
                  os.environ["BEFORE"], os.environ["GITHUB_SHA"],
                  "--", ".announcements/*.json",
              ])
              paths = [p for p in result.decode().split("\0") if p]
          entries = []
          for index, name in enumerate(paths):
              path = Path(name).resolve()
              if path.parent != root or path.suffix != ".json":
                  raise ValueError("Expected a JSON file directly under .announcements/")
              entries.append({
                  "name": f"announcement-{index}",
                  "content": json.loads(path.read_text(encoding="utf-8")),
              })
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
              output.write("entries=" + json.dumps(entries) + "\n")
          PY

  announce:
    needs: prepare
    if: needs.prepare.outputs.entries != '[]'
    strategy:
      fail-fast: false
      max-parallel: 1
      matrix:
        entry: ${{ fromJSON(needs.prepare.outputs.entries) }}
    uses: open-telemetry/shared-workflows/.github/workflows/announcements.yml@<commit-sha>
    with:
      announcement: ${{ toJSON(matrix.entry.content) }}
      channels: ${{ inputs.channels || vars.ANNOUNCEMENT_CHANNELS }}
      targets: ${{ vars.ANNOUNCEMENT_TARGETS }}
      mode: ${{ inputs.mode || 'draft' }}
      report_name: ${{ matrix.entry.name }}
      reddit_user_agent: "github-actions:otel-announcements:1.0 (by /u/YOUR_BOT)"
    secrets:
      BUFFER_API_KEY: ${{ secrets.BUFFER_API_KEY }}
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
      GROUPSIO_API_KEY: ${{ secrets.GROUPSIO_API_KEY }}
      REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}
      REDDIT_CLIENT_SECRET: ${{ secrets.REDDIT_CLIENT_SECRET }}
      REDDIT_REFRESH_TOKEN: ${{ secrets.REDDIT_REFRESH_TOKEN }}
```

Editing existing files does not trigger a new delivery in this example.
Do not later rerun a draft run in `publish` expecting draft promotion: `publish`
creates new deliveries. Approve existing drafts in Buffer/Groups.io manually,
and explicitly publish Slack/Reddit separately with only those services selected.

## Results, failures, and retries

Every invocation uploads a `report.json` artifact (14-day retention) and writes
status counts to the job summary. The report includes rendered content, targets,
planned actions, `preview`/`drafted`/`submitted`/`failed`/`pending` status, and any
returned remote IDs. Reports contain announcement content, so use only content
appropriate for the caller repository's artifact visibility. Credentials and
raw API responses are not included.

All content, destinations, and required secrets are validated before requests.
For non-preview Buffer destinations, channel/service identity and availability
are checked before the first write. Other service permission failures can still
occur during delivery. A destination failure is recorded, other destinations are
attempted, and the job fails overall. Reports are updated after each destination;
`pending` means the workflow did not finish an attempt for that destination.
Missing credentials or failed preflight checks prevent all delivery writes.

`submitted` means the service accepted the request, **not** confirmed end-to-end
publication. Buffer may complete publishing asynchronously or report a later
error; list moderation and Reddit moderation may delay or remove a submission.

There are **no automatic write retries and no persistent deduplication**.
Timeouts/connection errors can occur after a service accepted a write. Inspect
the report and remote services before retrying; select only destinations known
not to have received it. GitHub reruns, repeated manual invocations, and
concurrent callers can create duplicate messages or drafts. Consumers should
serialize publication events and track announced URLs if they need exactly-once
intent. This workflow cannot provide an atomic transaction across services.

## API references

- [Buffer draft creation](https://developers.buffer.com/examples/create-draft-post.html)
  and [post input](https://developers.buffer.com/types/CreatePostInput.html)
- [Slack chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage)
- [Groups.io API: drafts](https://groups.io/api#drafts)
- [Reddit API](https://www.reddit.com/dev/api/#POST_api_submit)

## Local development

The delivery implementation uses the Python standard library and runs on the
GitHub-hosted Ubuntu runner without additional packages.

```sh
python3 -m unittest discover -s .github/scripts/announcements -p 'test_*.py'
```

Tests mock all outbound requests and cover dry-run isolation, native drafts,
channel routing, service payloads, and partial-failure reporting.
