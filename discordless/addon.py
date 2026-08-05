"""Wirecord mitmproxy addon.

Intercepts Discord traffic (REST API + Gateway WebSocket), archives it to
``traffic_archive/`` in the same binary format as the original
``wumpus_in_the_middle.py`` (compatible with ``exporter.py``), and
optionally forwards messages from configured channels to a Discord webhook.

Usage::

    mitmdump -s discordless/addon.py \\
        --listen-port=8080 \\
        --allow-hosts '^(((.+\\.)?discord\\.com)|((.+\\.)?discordapp\\.com)|((.+\\.)?discord\\.net)|((.+\\.)?discordapp\\.net)|((.+\\.)?discord\\.gg))(?:\\:\\d+)?$'
"""
import os
import sys
import time

# Ensure the project root is on sys.path so 'discordless' is importable
# regardless of the working directory mitmdump is launched from.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:

    sys.path.insert(0, _PROJECT_ROOT)

from collections import OrderedDict
from typing import Dict, Set
from urllib.parse import urlparse

from mitmproxy import ctx, http  # type: ignore

from discordless.config import Config
from discordless.decoder import GatewayDecoder
from discordless.models import DiscordMessage
from discordless.native_forward import NativeForwarder, resolve_token
from discordless.webhook import WebhookForwarder

# Domains whose traffic is archived (mirrors wumpus_in_the_middle.py)
_MAX_TRACKED_MESSAGES = 1000

_DISCORD_DOMAINS: Set[str] = {
    "discord.com",
    "discordapp.com",
    "discord.net",
    "discordapp.net",
    "discord.gg",
    "dis.gd",
    "discord.co",
    "discord.app",
    "discord.dev",
    "discord.new",
    "discord.gift",
    "discord.gifts",
    "discord.media",
    "discord.store",
    "discordstatus.com",
    "bigbeans.solutions",
    "watchanimeattheoffice.com",
}

# Regex passed to --allow-hosts to restrict mitmproxy to Discord traffic only
ALLOW_HOSTS = (
    r"^(((.+\.)?discord\.com)|((.+\.)?discordapp\.com)"
    r"|((.+\.)?discord\.net)|((.+\.)?discordapp\.net)"
    r"|((.+\.)?discord\.gg))(?::\d+)?$"
)


def _is_discord(url: str) -> bool:
    hostname = urlparse(url).hostname or ""
    return any(hostname == d or hostname.endswith(f".{d}") for d in _DISCORD_DOMAINS)


def _is_gateway(url: str) -> bool:
    return _is_discord(url) and "gateway" in url.lower()


def _safe_filename(s: str) -> str:
    """Return a filesystem-safe version of *s*, max 255 chars."""
    return "".join(c if c.isalnum() or c == "." else "_" for c in s).rstrip()[:255]


def _log(msg: str) -> None:
    ctx.log.info(f"☎️  Wirecord: {msg}")


class _Gatekeeper:
    """Writes raw compressed Gateway chunks to ``{prefix}_data`` + ``{prefix}_timeline``."""

    def __init__(self, data_path: str, timeline_path: str) -> None:
        # "x" mode = exclusive create — raises FileExistsError if file already exists
        self._data = open(data_path, "xb")
        self._timeline = open(timeline_path, "x")

    def save(self, message: http.Message) -> None:  # type: ignore[name-defined]
        length = self._data.write(message.content)
        self._timeline.write(f"{message.timestamp} {length}\n")

    def close(self) -> None:
        self._data.close()
        self._timeline.close()


class WirecordAddon:
    """mitmproxy addon: archive Discord traffic and forward to webhook."""

    def __init__(self) -> None:
        self._config = Config()
        self._gatekeepers: Dict[int, _Gatekeeper] = {}
        self._decoders: Dict[int, GatewayDecoder] = {}
        self._seen_responses: Set[tuple] = set()
        self._seen_messages: Set[str] = set()
        self._gateway_count: int = 0
        self._forwarders: Dict[str, WebhookForwarder] = {}  # channel_id → forwarder
        # Maps (id(forwarder), discord_msg_id) → (webhook_msg_id, webhook_channel_id, guild_id)
        self._forwarded: OrderedDict = OrderedDict()
        self._channel_info: Dict[str, tuple] = {}  # channel_id → (channel_name, guild_name)
        self._archive: str = "traffic_archive"
        self._request_index = None
        self._gateway_index = None

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def running(self) -> None:
        """Called once the proxy is fully started and ready."""
        self._config = Config.load()
        self._archive = self._config.traffic_archive_dir

        requests_dir = os.path.join(self._archive, "requests")
        gateways_dir = os.path.join(self._archive, "gateways")
        os.makedirs(requests_dir, exist_ok=True)
        os.makedirs(gateways_dir, exist_ok=True)

        # Open index files in line-buffered mode so each write is flushed immediately
        self._request_index = open(
            os.path.join(self._archive, "request_index"), "a+", buffering=1
        )
        self._gateway_index = open(
            os.path.join(self._archive, "gateway_index"), "a+", buffering=1
        )

        # Rebuild dedup set from existing request_index
        self._request_index.seek(0)
        for line in self._request_index:
            parts = line.rstrip().split(maxsplit=4)
            if len(parts) == 5:
                _ts, _method, url, response_hash, _filename = parts
                self._seen_responses.add((url, response_hash))

        # Find next unused gateway sequence ID
        self._gateway_index.seek(0)
        self._gateway_count = max(
            (int(line.split()[-1]) + 1 for line in self._gateway_index if line.strip()),
            default=0,
        )

        # Resolve the account token once — only needed by native-forward rules.
        token = ""
        if self._config.native_enabled:
            token = resolve_token(self._config.user_token)
            if token:
                _log("native forward mode — account token resolved")
            else:
                _log(
                    "native forward mode requested but no account token found "
                    "(set 'user_token' in config.json) — those rules are skipped"
                )

        # Build channel → forwarder mapping from rules
        self._forwarders = {}
        native_count = 0
        for rule in self._config.forwards:
            if not rule.enabled:
                continue
            if rule.native:
                if not token:
                    continue
                fwd = NativeForwarder(
                    token=token,
                    dest_channel_id=rule.destination,
                    rate_limit_delay=rule.rate_limit_delay,
                )
                native_count += 1
            else:
                fwd = WebhookForwarder(
                    url=rule.webhook_url,
                    username=rule.webhook_username,
                    channel_id=rule.webhook_channel_id,
                    rate_limit_delay=rule.rate_limit_delay,
                )
            for ch in rule.channels:
                ch = str(ch)
                if ch in self._forwarders:
                    # Last rule wins — the earlier destination silently stops
                    # receiving this channel, which is easy to do by accident
                    # when adding a test rule for an already-monitored channel.
                    _log(f"WARNING: channel {ch} is claimed by several rules — last one wins")
                self._forwarders[ch] = fwd
        if self._forwarders:
            _log(
                f"forwarding enabled — {len(self._forwarders)} channel(s) monitored "
                f"({native_count} rule(s) in native forward mode)"
            )
        else:
            _log("forwarding disabled (configure 'forwards' in config.json)")

        _log(f"archiving to {os.path.abspath(self._archive)}/")
        _log(f"next gateway ID: {self._gateway_count}")

    def done(self) -> None:
        """Flush and close all open file handles."""
        if self._request_index:
            self._request_index.close()
        if self._gateway_index:
            self._gateway_index.close()
        for gk in self._gatekeepers.values():
            gk.close()
        unique_fwds = set(self._forwarders.values())
        if unique_fwds:
            sent = sum(f.stats["sent"] for f in unique_fwds)
            errors = sum(f.stats["errors"] for f in unique_fwds)
            _log(f"shutdown — forwarded {sent} message(s), {errors} error(s)")

    # ------------------------------------------------------------------
    # HTTP responses (REST API)
    # ------------------------------------------------------------------

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        """Stream file upload requests to avoid buffering large POST bodies."""
        if not _is_discord(flow.request.pretty_url):
            return
        if flow.request.method == "POST" and flow.request.pretty_url.endswith("/attachments"):
            _log("streaming attachment upload")
            flow.request.stream = True

    def response(self, flow: http.HTTPFlow) -> None:
        """Archive REST API responses, skipping exact duplicates."""
        url = flow.request.pretty_url
        if not _is_discord(url) or not flow.response.content:
            return

        response_hash = str(hash(flow.response.content))
        if (url, response_hash) in self._seen_responses:
            ctx.log.debug(f"☎️  Wirecord: skipping duplicate {url}")
            return

        filename = _safe_filename(
            f"{len(self._seen_responses)}_{url[8:].rsplit('?', maxsplit=1)[0]}"
        )
        dest = os.path.join(self._archive, "requests", filename)
        with open(dest, "wb") as f:
            f.write(flow.response.content)

        self._request_index.write(
            f"{flow.response.timestamp_start} {flow.request.method}"
            f" {url} {response_hash} {filename}\n"
        )
        self._seen_responses.add((url, response_hash))
        _log(f"archived {url}")

    # ------------------------------------------------------------------
    # WebSocket Gateway
    # ------------------------------------------------------------------

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """Archive each Gateway chunk and forward MESSAGE_CREATE events."""
        if not _is_gateway(flow.request.pretty_url):
            return

        message = flow.websocket.messages[-1]
        if message.from_client:
            return  # Only process server → client messages

        flow_key = id(flow)

        # Lazily initialise Gatekeeper + Decoder on first message for this flow
        if flow_key not in self._gatekeepers:
            prefix = str(self._gateway_count)
            self._gateway_count += 1
            gateways_dir = os.path.join(self._archive, "gateways")
            self._gatekeepers[flow_key] = _Gatekeeper(
                os.path.join(gateways_dir, f"{prefix}_data"),
                os.path.join(gateways_dir, f"{prefix}_timeline"),
            )
            self._decoders[flow_key] = GatewayDecoder(flow.request.pretty_url)
            ts = flow.response.timestamp_start if flow.response else time.time()
            self._gateway_index.write(
                f"{ts} {flow.request.pretty_url} {prefix}\n"
            )
            _log(f"new gateway connection #{prefix}")

        # Archive raw compressed chunk (preserves binary format for exporters)
        self._gatekeepers[flow_key].save(message)

        # Decode and optionally forward
        payload = self._decoders[flow_key].feed(message.content)
        if not isinstance(payload, dict):
            _log(f"DBG chunk={len(message.content)}b type={type(payload).__name__} first4={message.content[:4].hex()}")
            return
        t = payload.get("t")
        _log(f"DBG decoded t={t!r}")
        if t == "READY":
            self._index_channels_ready(payload.get("d", {}))
        elif t == "GUILD_CREATE":
            self._index_channels_guild(payload.get("d", {}))
        elif t in ("CHANNEL_CREATE", "CHANNEL_UPDATE"):
            self._index_channel(payload.get("d", {}))
        elif t == "MESSAGE_CREATE":
            _log(f"MESSAGE_CREATE channel={payload.get('d', {}).get('channel_id')}")
            self._maybe_forward(payload.get("d", {}))
        elif t == "MESSAGE_UPDATE":
            _log(f"MESSAGE_UPDATE channel={payload.get('d', {}).get('channel_id')}")
            self._maybe_forward_edit(payload.get("d", {}))

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        """Clean up state when a Gateway connection closes."""
        flow_key = id(flow)
        gk = self._gatekeepers.pop(flow_key, None)
        if gk:
            gk.close()
        self._decoders.pop(flow_key, None)
        _log("gateway connection closed")

    # ------------------------------------------------------------------
    # Forwarding
    # ------------------------------------------------------------------

    def _index_channels_guild(self, d: dict) -> None:
        """Build channel_id → (channel_name, guild_name) from a GUILD_CREATE payload."""
        guild_name = str(d.get("name", ""))
        for ch in d.get("channels", []):
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("id", ""))
            cname = str(ch.get("name", ""))
            if cid:
                self._channel_info[cid] = (cname, guild_name)

    def _index_channel(self, d: dict) -> None:
        """Update channel_id → (channel_name, guild_name) from a CHANNEL_CREATE/UPDATE payload."""
        cid = str(d.get("id", ""))
        cname = str(d.get("name", ""))
        if not cid or not cname:
            return
        _, existing_guild = self._channel_info.get(cid, ("", ""))
        self._channel_info[cid] = (cname, existing_guild)

    def _index_channels_ready(self, d: dict) -> None:
        """Build channel_id → (channel_name, guild_name) from a READY payload.

        In practice READY guilds are often unavailable stubs — real data comes
        via GUILD_CREATE events handled by :meth:`_index_channels_guild`.
        """
        for guild in d.get("guilds", []):
            if not isinstance(guild, dict):
                continue
            guild_name = str(guild.get("name", ""))
            for ch in guild.get("channels", []):
                if not isinstance(ch, dict):
                    continue
                cid = str(ch.get("id", ""))
                cname = str(ch.get("name", ""))
                if cid:
                    self._channel_info[cid] = (cname, guild_name)

    def _maybe_forward(self, d: dict) -> None:
        """Forward a MESSAGE_CREATE payload if it matches a configured channel."""
        channel_id = str(d.get("channel_id", ""))
        forwarder = self._forwarders.get(channel_id)
        if not forwarder:
            return

        author_data = d.get("author", {})
        if isinstance(author_data, dict):
            author = author_data.get("username", "unknown")
            author_id = str(author_data.get("id", ""))
            author_avatar = str(author_data.get("avatar", "") or "")
        else:
            author, author_id, author_avatar = "unknown", "", ""
        content = str(d.get("content", "")).strip()
        timestamp = str(d.get("timestamp", ""))
        discord_msg_id = str(d.get("id", ""))
        source_guild_id = str(d.get("guild_id") or "")
        native = getattr(forwarder, "is_native", False)

        attachments = d.get("attachments", [])
        if native:
            # A native forward references the source message, so Discord renders
            # its attachments and embeds itself — pasting URLs would only add
            # noise the original message never had.
            has_media = bool(attachments or d.get("embeds") or d.get("sticker_items"))
            if not content and not has_media:
                return  # Nothing visible to forward
            if not discord_msg_id:
                return  # Cannot reference a message without its ID
        else:
            # Append attachment URLs to content so files/images are forwarded
            if isinstance(attachments, list):
                for att in attachments:
                    if isinstance(att, dict):
                        url = att.get("url", "")
                        if url:
                            content = f"{content}\n{url}" if content else url
            if not content:
                return  # Skip embed-only / empty messages

        channel_name, guild_name = self._channel_info.get(channel_id, ("", ""))
        msg = DiscordMessage(
            channel_id=channel_id,
            author=author,
            content=content,
            timestamp=timestamp,
            channel_name=channel_name,
            guild_name=guild_name,
            author_id=author_id,
            author_avatar=author_avatar,
            message_id=discord_msg_id,
            guild_id=source_guild_id,
        )

        if msg.dedup_key in self._seen_messages:
            return
        self._seen_messages.add(msg.dedup_key)

        result = forwarder.forward_and_get_id(msg)
        if result and discord_msg_id:
            webhook_msg_id, webhook_channel_id, guild_id = result
            # Prefer guild_id from source message if webhook response has none
            guild_id = guild_id or source_guild_id
            key = (id(forwarder), discord_msg_id)
            if len(self._forwarded) >= _MAX_TRACKED_MESSAGES:
                self._forwarded.popitem(last=False)  # FIFO eviction
            self._forwarded[key] = (webhook_msg_id, webhook_channel_id, guild_id)
            label = "forward" if native else "webhook msg"
            _log(f"forwarded {discord_msg_id} → {label} {webhook_msg_id}")

    def _maybe_forward_edit(self, d: dict) -> None:
        """Send an edit-notification if the edited message was previously forwarded."""
        channel_id = str(d.get("channel_id", ""))
        forwarder = self._forwarders.get(channel_id)
        if not forwarder:
            return
        if not getattr(forwarder, "supports_edits", True):
            # A native forward is an immutable snapshot: Discord offers no way to
            # update it, and posting a separate "edited" notice would expose the
            # relay as automated. Edits are intentionally dropped in native mode.
            return

        discord_msg_id = str(d.get("id", ""))
        if not discord_msg_id:
            return

        tracked = self._forwarded.get((id(forwarder), discord_msg_id))
        if not tracked:
            return  # Not forwarded in this session

        webhook_msg_id, webhook_channel_id, guild_id = tracked

        author_data = d.get("author", {})
        if isinstance(author_data, dict):
            author = author_data.get("username", "unknown")
            author_id = str(author_data.get("id", ""))
            author_avatar = str(author_data.get("avatar", "") or "")
        else:
            author, author_id, author_avatar = "unknown", "", ""

        new_content = str(d.get("content", "")).strip()
        if not new_content:
            return  # Embed-only edit — skip

        forwarder.forward_edit_notification(
            original_msg_id=webhook_msg_id,
            webhook_channel_id=webhook_channel_id,
            guild_id=guild_id,
            new_content=new_content,
            author=author,
            author_id=author_id,
            author_avatar=author_avatar,
        )
        _log(f"edit-notification sent for discord msg {discord_msg_id}")


addons = [WirecordAddon()]
