# Wirecord

A fork of [discordless](https://github.com/Roachbones/discordless).

Intercept and archive Discord traffic through a [mitmproxy](https://mitmproxy.org/) addon, with optional real-time message forwarding to Discord webhooks.

## What it does

Wirecord sits between your Discord client and Discord's servers as a transparent proxy. It:

- **Archives all traffic** — REST API responses and Gateway WebSocket data are saved in raw format to `traffic_archive/`.
- **Forwards messages** — Messages from configured channels are relayed to Discord webhooks in real time, with the original author's username, avatar, and channel name.
- **Exports archives** — Saved traffic can be exported to DiscordChatExporter-compatible JSON or HTML.

## Quick start

### Requirements

- Python 3.10+
- [mitmproxy](https://mitmproxy.org/)

### Install

```bash
git clone <your-repo-url>
cd Wirecord
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Install [mitmproxy's CA certificate](https://docs.mitmproxy.org/stable/concepts-certificates/#quick-setup) on every device you want to archive.

### Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with your settings:

```json
{
  "proxy_port": 8080,
  "traffic_archive_dir": "traffic_archive",
  "forwards": [
    {
      "channels": ["SOURCE_CHANNEL_ID"],
      "webhook_url": "https://discord.com/api/webhooks/ID/TOKEN",
      "webhook_channel_id": "",
      "webhook_username": "Interceptor",
      "rate_limit_delay": 0.5
    }
  ]
}
```

| Field | Description |
|---|---|
| `channels` | Source channel IDs to intercept |
| `webhook_url` | Discord webhook URL for the destination (webhook mode) |
| `webhook_channel_id` | Thread/forum channel ID (leave empty for regular text channels) |
| `webhook_username` | Fallback display name (overridden by original author name) |
| `rate_limit_delay` | Minimum seconds between requests |
| `forward_mode` | `webhook` (default) or `native` — overrides the global mode for this rule |
| `dest_channel_id` | Destination channel for native mode (defaults to `webhook_channel_id`) |

You can define multiple forwarding rules to relay different channels to different webhooks.

#### Native forward mode

Setting `forward_mode` to `native` — globally or per rule — relays messages using
Discord's own **Forward** feature instead of a webhook post:

```json
{
  "forward_mode": "native",
  "user_token": "",
  "forwards": [
    {
      "channels": ["SOURCE_CHANNEL_ID"],
      "dest_channel_id": "DESTINATION_CHANNEL_ID",
      "rate_limit_delay": 1.5
    }
  ]
}
```

A native forward is created with `message_reference.type = 1` and an **account
token** — webhooks cannot produce one, as the webhook endpoint rejects
`message_reference`. Leave `user_token` empty to auto-detect the token from the
local Discord client (same source as the replay scripts); it is never logged.

| | Webhook mode | Native mode |
|---|---|---|
| Posted as | The webhook, with an `APP` badge | Your account, no badge |
| Display name | Original author + source channel | Your account name |
| Attachments | Appended as plain URLs | Rendered natively by Discord |
| Embeds / stickers | Not relayed | Rendered natively by Discord |
| Text-free messages | Skipped | Forwarded |
| Message edits | Relayed as an edit notice | Not relayed (a forward is an immutable snapshot) |
| Rate limit | 0.5 s is fine | Floored to 1.0 s (5 posts / 5 s per channel) |

Native mode posts under your own account, which Discord's terms treat as
self-botting — the risk falls on that account.

### Run

```bash
scripts/start.sh    # Start proxy + Discord
scripts/stop.sh     # Stop both
```

Or run the proxy manually:

```bash
mitmdump -s discordless/addon.py --listen-port=8080 \
  --allow-hosts '^(((.+\.)?discord\.com)|((.+\.)?discordapp\.com)|((.+\.)?discord\.net)|((.+\.)?discordapp\.net)|((.+\.)?discord\.gg))(?::\d+)?$'
```

Then start Discord with the proxy:

```bash
discord --proxy-server=localhost:8080
```

### Docker

```bash
docker compose up --build
```

## Forwarded messages

In **webhook mode** (default), messages from monitored channels appear in the destination with:

- The original author's **username** and **avatar**
- The source **channel name** next to the author
- **Attachments** (images, files) as clickable URLs
- **Custom emojis** are forwarded as-is in the text (they will only render if the webhook's server has access to the same emojis)

In **native mode**, the message is relayed as a genuine Discord forward: the client
renders the original author, text, attachments and embeds from the forwarded
snapshot, under your own account name.

## Replaying missed messages

The proxy only captures traffic while the Discord client is running and routed through it. If the client is stopped (or not proxied), messages in monitored channels are missed and never forwarded. The `scripts/replay_*` tools fill that gap by listing and re-posting missed messages using the Discord API + the configured webhooks.

| Script | Action |
|---|---|
| `scripts/replay_api_dryrun.py` | Lists missed messages per channel (API-based) — posts nothing |
| `scripts/replay_api_send.py` | Re-posts missed messages via webhooks — the actual send |
| `scripts/replay_missing_dryrun.py` | Dry-run based on captured archives only (sees only intercepted messages) |

### How it works

1. For each source channel, the script computes the last forwarded/sent message ID:
   - live-forwarded IDs are parsed from `logs/mitmdump.log` (`forwarded <id> ...`)
   - replay-sent IDs are tracked per channel in `scripts/.replay_sent_<channel>.txt`
   - the resume point is `max(live-forwarded, replay-sent)`
2. It fetches every message newer than that point via the Discord API (`GET /channels/{id}/messages?after=...`, paginated toward the most recent).
3. It filters out already-forwarded/sent messages and empty (embed-only) messages, then posts the rest to the configured webhook (`?thread_id=<thread>` for forum/thread destinations), with the original author's username/avatar and `split_content` for the 2000-char limit.
4. Each successfully posted message ID is appended to `scripts/.replay_sent_<channel>.txt`, so the next run resumes after it — no duplicates, no re-posting of history.

### Usage

```bash
PYTHONPATH=. venv/bin/python scripts/replay_api_dryrun.py   # list missed (no send)
PYTHONPATH=. venv/bin/python scripts/replay_api_send.py     # send missed
```

### Notes & guardrails

- **Auth**: the Discord user token is read from `~/.config/discord/Local Storage/leveldb/*` at runtime and never printed.
- **Stop on HTTP 400**: if a destination thread is invalid (`10003 Unknown Channel`), the script stops that channel to avoid spam — fix the thread ID in `config.json` and rerun.
- **Channel selection**: which channels to send is set in `TARGETS` / `AFTER` at the top of `replay_api_send.py`. To skip a channel (e.g. CIG Comms), simply omit it from `TARGETS`.
- **Rate limiting**: 1.2 s between posts + 429 retry with `retry_after`.
- The archive-based dry-run (`replay_missing_dryrun.py`) only sees messages that were actually intercepted — it cannot reveal messages missed while the client was down. Use the API-based scripts for that.

## Exporting archives

Export saved traffic from `traffic_archive/` to readable formats:

```bash
python3 exporter.py dcejson-exporter    # DiscordChatExporter-compatible JSON
python3 exporter.py html-exporter       # HTML
python3 exporter.py htmeml-exporter     # Memory-efficient paginated HTML
python3 exporter.py <name> -h           # See all options
```

JSON exports are compatible with [DiscordChatExporter-frontend](https://github.com/slatinsky/DiscordChatExporter-frontend) and [chat-analytics](https://github.com/mlomb/chat-analytics).

## Architecture

```
Discord client
    |  (proxy)
mitmproxy + discordless/addon.py
    |-- REST responses  --> traffic_archive/requests/
    |-- Gateway chunks  --> traffic_archive/gateways/
    '-- MESSAGE_CREATE  --> WebhookForwarder --> Discord webhook
```

### Core package: `discordless/`

| File | Role |
|---|---|
| `addon.py` | mitmproxy addon — intercepts, archives, and forwards |
| `config.py` | Loads `config.json` into typed dataclasses |
| `models.py` | `DiscordMessage` — immutable message representation |
| `decoder.py` | `GatewayDecoder` — stateful zlib/zstd + JSON/ETF decoder per WebSocket connection |
| `webhook.py` | `WebhookForwarder` — POST to Discord webhooks with rate limiting |

### Export pipeline: `exporters/`

| Exporter | Output |
|---|---|
| `dcejson/` | DiscordChatExporter-compatible JSON |
| `html/` | Classical HTML chatlog |
| `htmeml/` | Memory-efficient paginated HTML |

### Key technical details

- Gateway data is archived as **raw compressed binary** — exporters can replay and decode it later
- `GatewayDecoder` maintains a **stateful decompressor** per connection (Discord's `zstd-stream` is a continuous stream, not independent frames)
- Message deduplication uses an in-memory set keyed by MD5 of timestamp + author + content
- The proxy only intercepts Discord domains — other traffic passes through untouched

## Testing

```bash
PYTHONPATH=. venv/bin/python -m pytest                          # All tests
PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_config.py  # One file
PYTHONPATH=. venv/bin/python -m pytest -k "test_name"             # By name
```

### Testing with recorded traffic

```bash
# Record
mitmproxy -w discord_dump.flow --set stream_large_bodies=100k \
  --allow-hosts '<discord regex>'

# Replay
mitmdump -s discordless/addon.py --rfile discord_dump.flow
```

## Limitations

- **iOS WebSocket traffic** ignores HTTP proxy settings, so Gateway data from iOS devices is not captured. REST traffic still works.
- **Custom emojis** from other servers won't render in forwarded messages (Discord limitation).
- **Multiple Discord accounts** through the same proxy instance are not supported — all traffic is treated as one account.

## License

MIT
