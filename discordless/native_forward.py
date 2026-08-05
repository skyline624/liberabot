"""Native forwarder — re-posts captured messages using Discord's own Forward feature.

A native forward is created by ``POST /channels/{id}/messages`` with
``message_reference.type = 1`` and an account token in ``Authorization``.
Discord then renders a genuine "Forwarded" block from ``message_snapshots``:
the original author, attachments and embeds appear natively, and the post
carries no ``APP`` badge because it comes from the account itself.

Webhooks cannot produce this — the webhook endpoint rejects ``message_reference``,
which is why this is a separate transport from :mod:`discordless.webhook`.

Delivery is asynchronous: :meth:`NativeForwarder.enqueue` returns immediately and
a background worker waits a per-message random delay before posting, so a
deliberate delay never blocks mitmproxy's traffic loop. Each post is sent by an
account picked at random from the rule's pool — both the timing and the sender
vary from one message to the next.

Trade-offs versus webhook mode:
  * no per-rule ``webhook_username`` / avatar — the post shows the account
  * message edits cannot be reflected (a forward is an immutable snapshot)
  * stricter rate limit (5 posts / 5 s per channel) than a webhook
"""
import base64
import glob
import json
import os
import queue
import random
import re
import threading
import time

import requests  # type: ignore

from discordless.models import DiscordMessage

API_BASE = "https://discord.com/api/v9"

# Discord's own snowflake epoch — used to build a client-style nonce.
_DISCORD_EPOCH = 1420070400000

# message_reference.type — 0 = reply (default), 1 = forward.
_REFERENCE_TYPE_FORWARD = 1

# POST /channels/{id}/messages allows 5 requests / 5 s per channel. Going faster
# only produces 429s, so any configured delay is floored to this value.
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


def _collect_tokens() -> list:
    """Return the distinct account tokens found in the local Discord leveldb.

    Order is preserved (first store, first occurrence first) so the no-``user_id``
    path keeps its historical "first token wins" behaviour.
    """
    tokens: list = []
    seen: set = set()
    for path in sorted(glob.glob(os.path.expanduser(_LEVELDB_GLOB))):
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        for match in _TOKEN_RE.finditer(blob):
            tok = match.group().decode()
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    return tokens


def client_headers(token: str) -> dict:
    """Return the request headers used to talk to the API as *token*."""
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "X-Discord-Locale": "fr-FR",
        "X-Super-Properties": _SUPER_PROPERTIES,
    }


def account_id(token: str, api_base: str = API_BASE) -> str:
    """Return the Discord user id owning *token*, or "" if it cannot be resolved."""
    try:
        resp = requests.get(
            f"{api_base}/users/@me", headers=client_headers(token), timeout=15
        )
    except requests.RequestException:
        return ""
    if resp.status_code != 200:
        return ""
    try:
        return str(resp.json().get("id") or "")
    except ValueError:
        return ""


def resolve_token(explicit: str = "", user_id: str = "") -> str:
    """Return the account token to authenticate forwards with.

    Args:
        explicit: Token from ``config.json``; returned as-is when non-empty.
        user_id: When set, pick the stored token whose account has this id, so
            the posting account stays fixed even if the leveldb ordering changes
            or a second account is added to the client. No fallback to another
            account when the requested one is absent — posting as the wrong
            account would be worse than not posting.

    Returns:
        The explicit token; else the token matching *user_id*; else the first
        token found in the local Discord leveldb store (same source as
        ``scripts/replay_api_send.py``); else "".
    """
    if explicit:
        return explicit.strip()
    tokens = _collect_tokens()
    if user_id:
        for tok in tokens:
            if account_id(tok) == str(user_id):
                return tok
        return ""  # requested account not present — do not fall back silently
    return tokens[0] if tokens else ""


def tokens_for_ids(user_ids, explicit_by_id=None) -> dict:
    """Resolve a set of account ids to ``{user_id: token}`` from the leveldb store.

    Only ids actually found are returned, so the caller can report the missing
    ones. ``explicit_by_id`` (optional ``{id: token}``) takes precedence over the
    leveldb lookup for any id it covers — used when a token is pinned in config.
    """
    wanted = {str(u) for u in (user_ids or [])}
    if not wanted:
        return {}
    explicit_by_id = {str(k): v for k, v in (explicit_by_id or {}).items()}
    found: dict = {}
    for uid in wanted & set(explicit_by_id):
        found[uid] = explicit_by_id[uid]
    remaining = wanted - set(found)
    if remaining:
        for tok in _collect_tokens():
            uid = account_id(tok)
            if uid in remaining and uid not in found:
                found[uid] = tok
                remaining.discard(uid)
                if not remaining:
                    break
    return found


def _nonce() -> str:
    """Build a client-style snowflake nonce for a new message."""
    return str((int(time.time() * 1000) - _DISCORD_EPOCH) << 22)


class NativeForwarder:
    """Asynchronously posts real Discord forwards into a destination channel.

    Interchangeable with :class:`~discordless.webhook.WebhookForwarder` in
    :mod:`discordless.addon` via the ``is_native`` / ``supports_edits`` capability
    flags the addon branches on. Unlike the webhook forwarder it delivers off the
    caller's thread: :meth:`enqueue` returns at once and a background worker paces
    and posts each message.

    Attributes:
        accounts: ``{user_id: token}`` pool; each post is sent by a random one.
        dest_channel_id: Channel (or thread) ID forwards are posted to.
        delay_min/delay_max: Bounds of the random per-message delay (seconds).
        disabled: user_ids dropped after a 401, to stop hammering the API.
        stats: Running counters — ``sent``, ``errors`` and ``dropped``.
    """

    is_native = True
    supports_edits = False  # a forward is an immutable snapshot

    def __init__(
        self,
        accounts,
        dest_channel_id: str,
        delay_min: float = _MIN_RATE_LIMIT_DELAY,
        delay_max=None,
        api_base: str = API_BASE,
        start_worker: bool = True,
    ) -> None:
        # A bare token string is accepted for convenience (single-account pool).
        if isinstance(accounts, str):
            accounts = {"": accounts}
        self.accounts: dict = dict(accounts)
        self.dest_channel_id = str(dest_channel_id)
        self.delay_min = max(float(delay_min), _MIN_RATE_LIMIT_DELAY)
        self.delay_max = (
            self.delay_min if delay_max is None else max(float(delay_max), self.delay_min)
        )
        self.api_base = api_base
        self.disabled: set = set()
        self.stats: dict = {"sent": 0, "errors": 0, "dropped": 0}
        self._q: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._worker = None
        if start_worker and self.accounts:
            self._worker = threading.Thread(
                target=self._run, name=f"native-fwd-{self.dest_channel_id}", daemon=True
            )
            self._worker.start()

    @property
    def url(self) -> str:
        """Endpoint that forwards are POSTed to."""
        return f"{self.api_base}/channels/{self.dest_channel_id}/messages"

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _pick_delay(self) -> float:
        """Random delay in ``[delay_min, delay_max]`` — different (almost) every time."""
        if self.delay_max <= self.delay_min:
            return self.delay_min
        return random.uniform(self.delay_min, self.delay_max)

    def _live_accounts(self) -> list:
        return [(uid, tok) for uid, tok in self.accounts.items() if uid not in self.disabled]

    def _pick_account(self):
        """A random (user_id, token) among accounts not disabled, or None."""
        live = self._live_accounts()
        return random.choice(live) if live else None

    # ------------------------------------------------------------------
    # Enqueue + worker
    # ------------------------------------------------------------------

    def enqueue(self, msg: DiscordMessage) -> None:
        """Queue *msg* for asynchronous forwarding; returns immediately."""
        if not self.accounts:
            self.stats["dropped"] += 1
            return
        if not msg.message_id:
            self.stats["errors"] += 1
            self._log_warn("☎️  Wirecord: native forward skipped — source message id missing")
            return
        self._q.put(msg)

    def _run(self) -> None:
        """Worker loop: wait a random delay per message, then post it."""
        while not self._stop.is_set():
            try:
                msg = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                # Interruptible wait — a shutdown cuts it short and the message
                # is still posted below rather than lost.
                self._stop.wait(self._pick_delay())
                self._send(msg)
            except Exception as e:  # never let the worker die on one message
                self.stats["errors"] += 1
                self._log_warn(f"☎️  Wirecord: native forward worker error: {e}")
            finally:
                self._q.task_done()

    def close(self, timeout=None) -> None:
        """Stop the worker and flush any queued messages without further delay."""
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout if timeout is not None else self.delay_max + 12)
        while True:  # drain whatever is left, posting immediately
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                self._send(msg)
            except Exception as e:
                self.stats["errors"] += 1
                self._log_warn(f"☎️  Wirecord: native forward flush error: {e}")
            finally:
                self._q.task_done()

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

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

    def _send(self, msg: DiscordMessage):
        """Post *msg* once, with a random live account. Returns the new id or None."""
        acct = self._pick_account()
        if acct is None:
            self.stats["errors"] += 1
            self._log_warn("☎️  Wirecord: native forward has no usable account")
            return None
        uid, token = acct
        resp = self._post_with_retry(self._payload(msg), token)
        if resp is None:
            self.stats["errors"] += 1
            return None
        if resp.status_code in (200, 201):
            self.stats["sent"] += 1
            try:
                data = resp.json()
            except ValueError:
                data = {}
            self._log_info(
                f"☎️  Wirecord: forwarded {msg.message_id} as {uid or 'account'} "
                f"→ {data.get('id')}"
            )
            return str(data.get("id") or "")
        self.stats["errors"] += 1
        if resp.status_code == 401:
            self.disabled.add(uid)
            self._log_warn(
                f"☎️  Wirecord: native forward unauthorized (401) for account {uid} — "
                "removed from the pool until restart"
            )
        else:
            self._log_warn(
                f"☎️  Wirecord: native forward HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return None

    def _post_with_retry(self, payload: dict, token: str, max_retries: int = 5):
        """POST one forward with *token*, retrying on HTTP 429. Returns the response."""
        resp = None
        for _ in range(max_retries):
            try:
                resp = requests.post(
                    self.url, json=payload, headers=client_headers(token), timeout=10
                )
            except requests.RequestException as e:
                self._log_warn(f"☎️  Wirecord: native forward request failed: {e}")
                return None
            if resp.status_code != 429:
                return resp
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            self._log_warn(f"☎️  Wirecord: native forward 429 — retrying after {retry_after}s")
            time.sleep(retry_after + 0.3)
        return resp  # exhausted retries — still 429

    # ------------------------------------------------------------------
    # Logging (best-effort via mitmproxy ctx, no-op outside mitmdump)
    # ------------------------------------------------------------------

    @staticmethod
    def _log_warn(message: str) -> None:
        try:
            from mitmproxy import ctx  # type: ignore
            ctx.log.warn(message)
        except Exception:
            pass

    @staticmethod
    def _log_info(message: str) -> None:
        try:
            from mitmproxy import ctx  # type: ignore
            ctx.log.info(message)
        except Exception:
            pass
