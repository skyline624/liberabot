"""Native forwarder — re-posts captured messages using Discord's own Forward feature.

A native forward is created by ``POST /channels/{id}/messages`` with
``message_reference.type = 1`` and an account token in ``Authorization``.
Discord then renders a genuine "Forwarded" block from ``message_snapshots``:
the original author, attachments and embeds appear natively, and the post
carries no ``APP`` badge because it comes from the account itself.

Webhooks cannot produce this — the webhook endpoint rejects ``message_reference``,
which is why this is a separate transport from :mod:`discordless.webhook`.

Trade-offs versus webhook mode:
  * no per-rule ``webhook_username`` / avatar — the post shows the account
  * message edits cannot be reflected (a forward is an immutable snapshot)
  * stricter rate limit (5 posts / 5 s per channel) than a webhook
"""
import base64
import glob
import json
import os
import re
import time

import requests  # type: ignore

from discordless.models import DiscordMessage

API_BASE = "https://discord.com/api/v9"

# Discord's own snowflake epoch — used to build a client-style nonce.
_DISCORD_EPOCH = 1420070400000

# message_reference.type — 0 = reply (default), 1 = forward.
_REFERENCE_TYPE_FORWARD = 1

# POST /channels/{id}/messages allows 5 requests / 5 s per channel. Going faster
# only produces 429s, so the configured delay is floored to this value.
_MIN_RATE_LIMIT_DELAY = 1.0

_TOKEN_RE = re.compile(rb"[A-Za-z0-9_=-]{24}\.[A-Za-z0-9_=-]{6}\.[A-Za-z0-9_=-]{27,}")
_LEVELDB_GLOB = "~/.config/discord/Local Storage/leveldb/*"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "discord/0.0.109 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36"
)

_SUPER_PROPERTIES = base64.b64encode(
    json.dumps(
        {
            "os": "Linux",
            "browser": "Discord Client",
            "release_channel": "stable",
            "client_version": "0.0.109",
            "os_version": "6.1.0",
            "system_locale": "fr-FR",
            "client_build_number": 366177,
            "client_event_source": None,
        },
        separators=(",", ":"),
    ).encode()
).decode()


def resolve_token(explicit: str = "") -> str:
    """Return the account token to authenticate forwards with.

    Args:
        explicit: Token from ``config.json``; returned as-is when non-empty.

    Returns:
        The explicit token, else one recovered from the local Discord client's
        leveldb store (same source as ``scripts/replay_api_send.py``), else "".
    """
    if explicit:
        return explicit.strip()
    for path in sorted(glob.glob(os.path.expanduser(_LEVELDB_GLOB))):
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        match = _TOKEN_RE.search(blob)
        if match:
            return match.group().decode()
    return ""


def _nonce() -> str:
    """Build a client-style snowflake nonce for a new message."""
    return str((int(time.time() * 1000) - _DISCORD_EPOCH) << 22)


def client_headers(token: str) -> dict:
    """Return the request headers used to talk to the API as *token*."""
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "X-Discord-Locale": "fr-FR",
        "X-Super-Properties": _SUPER_PROPERTIES,
    }


class NativeForwarder:
    """Posts real Discord forwards of captured messages into a destination channel.

    Interchangeable with :class:`~discordless.webhook.WebhookForwarder` in
    :mod:`discordless.addon`: same ``stats`` counters and ``forward_and_get_id``
    contract, plus the ``is_native`` / ``supports_edits`` capability flags the
    addon branches on.

    Attributes:
        dest_channel_id: Channel (or thread) ID forwards are posted to.
        rate_limit_delay: Minimum seconds between consecutive posts.
        disabled: Set once the token is rejected, to stop hammering the API.
        stats: Running counters — ``sent`` and ``errors``.
    """

    is_native = True
    supports_edits = False  # a forward is an immutable snapshot

    def __init__(
        self,
        token: str,
        dest_channel_id: str,
        rate_limit_delay: float = _MIN_RATE_LIMIT_DELAY,
        api_base: str = API_BASE,
    ) -> None:
        self.token = token
        self.dest_channel_id = str(dest_channel_id)
        self.rate_limit_delay = max(float(rate_limit_delay), _MIN_RATE_LIMIT_DELAY)
        self.api_base = api_base
        self.disabled = False
        self._last_sent: float = 0.0
        self.stats: dict = {"sent": 0, "errors": 0}

    @property
    def url(self) -> str:
        """Endpoint that forwards are POSTed to."""
        return f"{self.api_base}/channels/{self.dest_channel_id}/messages"

    def _headers(self) -> dict:
        return client_headers(self.token)

    @staticmethod
    def _log_warn(message: str) -> None:
        """Best-effort warning log via mitmproxy ctx (no-op outside mitmdump)."""
        try:
            from mitmproxy import ctx  # type: ignore
            ctx.log.warn(message)
        except Exception:
            pass

    def _wait_for_rate_limit(self) -> None:
        """Block until at least :attr:`rate_limit_delay` has passed since the last post."""
        elapsed = time.time() - self._last_sent
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)

    def _payload(self, msg: DiscordMessage) -> dict:
        """Build the forward payload for *msg*.

        ``content`` stays empty: the forwarded snapshot already carries the text,
        attachments and embeds, so adding a caption would be the one visible
        giveaway that the post is automated.
        """
        reference = {
            "type": _REFERENCE_TYPE_FORWARD,
            "channel_id": str(msg.channel_id),
            "message_id": str(msg.message_id),
        }
        if msg.guild_id:
            reference["guild_id"] = str(msg.guild_id)
        return {
            "content": "",
            "flags": 0,
            "tts": False,
            "nonce": _nonce(),
            "message_reference": reference,
        }

    def _post_with_retry(self, payload: dict, max_retries: int = 5):
        """POST one forward, pacing per :attr:`rate_limit_delay` and retrying on 429.

        Returns the final :class:`requests.Response`, or ``None`` on a network error.
        """
        resp = None
        for _ in range(max_retries):
            self._wait_for_rate_limit()
            try:
                resp = requests.post(
                    self.url, json=payload, headers=self._headers(), timeout=10
                )
            except requests.RequestException as e:
                self._last_sent = time.time()
                self._log_warn(f"☎️  Wirecord: native forward request failed: {e}")
                return None
            self._last_sent = time.time()
            if resp.status_code != 429:
                return resp
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            self._log_warn(f"☎️  Wirecord: native forward 429 — retrying after {retry_after}s")
            time.sleep(retry_after + 0.3)
        return resp  # exhausted retries — still 429

    def forward_and_get_id(self, msg: DiscordMessage) -> tuple | None:
        """Forward *msg* natively into the destination channel.

        Args:
            msg: Captured message; :attr:`~discordless.models.DiscordMessage.message_id`
                must be set, since the forward references the source message rather
                than re-sending its content.

        Returns:
            ``(created_msg_id, channel_id, guild_id)`` on success, ``None`` otherwise.
        """
        if self.disabled:
            return None
        if not msg.message_id:
            self.stats["errors"] += 1
            self._log_warn("☎️  Wirecord: native forward skipped — source message id missing")
            return None

        resp = self._post_with_retry(self._payload(msg))
        if resp is None:
            self.stats["errors"] += 1
            return None

        if resp.status_code in (200, 201):
            self.stats["sent"] += 1
            try:
                data = resp.json()
            except ValueError:
                data = {}
            return (
                str(data.get("id") or ""),
                str(data.get("channel_id") or self.dest_channel_id),
                str(data.get("guild_id") or ""),
            )

        self.stats["errors"] += 1
        if resp.status_code == 401:
            self.disabled = True
            self._log_warn(
                "☎️  Wirecord: native forward unauthorized (401) — token invalid or expired; "
                "this rule is disabled until restart"
            )
        else:
            self._log_warn(
                f"☎️  Wirecord: native forward HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return None

    def forward(self, msg: DiscordMessage) -> bool:
        """Forward *msg*, returning True on success."""
        return self.forward_and_get_id(msg) is not None
