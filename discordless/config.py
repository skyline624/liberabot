"""Configuration loading for Wirecord.

The config file is a JSON file (default: config.json) at the project root.
Keys starting with '_' are treated as comments and ignored.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


DEFAULT_CONFIG_PATH = "config.json"

# Delivery modes for a forwarding rule.
MODE_WEBHOOK = "webhook"  # POST to a webhook URL as a rich message (default)
MODE_NATIVE = "native"    # POST as a real Discord "Forward" using an account token
VALID_MODES = (MODE_WEBHOOK, MODE_NATIVE)


@dataclass
class ForwardRule:
    """One forwarding rule: N source channels → one destination.

    Attributes:
        channels: Discord channel IDs to intercept.
        webhook_url: Discord webhook URL (webhook mode only).
        webhook_channel_id: Destination thread ID, when the target is a thread.
        webhook_username: Display name shown on forwarded messages (webhook mode only).
        rate_limit_delay: Minimum seconds between consecutive POSTs.
        forward_mode: ``webhook`` or ``native``; inherits the global mode when unset.
        dest_channel_id: Destination channel for native mode; falls back to
            ``webhook_channel_id`` so existing rules work unchanged.
    """

    channels: List[str] = field(default_factory=list)
    webhook_url: str = ""
    webhook_channel_id: str = ""
    webhook_username: str = "Interceptor"
    rate_limit_delay: float = 0.5
    forward_mode: str = MODE_WEBHOOK
    dest_channel_id: str = ""

    @classmethod
    def from_dict(cls, data: dict, default_mode: str = MODE_WEBHOOK) -> "ForwardRule":
        """Build a rule from raw JSON, inheriting *default_mode* when unspecified."""
        known = cls.__dataclass_fields__
        rule = cls(**{k: v for k, v in data.items() if k in known})
        if not data.get("forward_mode"):
            rule.forward_mode = default_mode
        rule.forward_mode = str(rule.forward_mode).lower()
        if rule.forward_mode not in VALID_MODES:
            rule.forward_mode = MODE_WEBHOOK
        return rule

    @property
    def native(self) -> bool:
        """True when this rule posts real Discord forwards instead of webhook messages."""
        return self.forward_mode == MODE_NATIVE

    @property
    def destination(self) -> str:
        """Channel (or thread) ID that native forwards are posted to."""
        return str(self.dest_channel_id or self.webhook_channel_id or "")

    @property
    def enabled(self) -> bool:
        """True when the rule has everything its mode requires."""
        if not self.channels:
            return False
        if self.native:
            return bool(self.destination)
        return bool(self.webhook_url)


@dataclass
class Config:
    """Wirecord runtime configuration.

    Attributes:
        proxy_port: Port for the mitmproxy proxy server.
        traffic_archive_dir: Directory where raw captured traffic is stored.
        forward_mode: Default delivery mode for every rule (``webhook`` or ``native``).
        user_token: Discord account token for native mode. Left empty, the token
            is auto-detected from the local Discord client's leveldb store.
        forwards: List of forwarding rules (channels → destination).
    """

    proxy_port: int = 8080
    traffic_archive_dir: str = "traffic_archive"
    forward_mode: str = MODE_WEBHOOK
    user_token: str = ""
    forwards: List[ForwardRule] = field(default_factory=list)

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        """Load configuration from a JSON file.

        Falls back to default values for a missing or malformed file.

        Args:
            path: Path to the JSON config file.

        Returns:
            Config instance populated from the file.
        """
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = {k: v for k, v in data.items() if not k.startswith("_")}
            mode = str(data.get("forward_mode", MODE_WEBHOOK)).lower()
            if mode not in VALID_MODES:
                mode = MODE_WEBHOOK
            forwards = [
                ForwardRule.from_dict(r, mode)
                for r in data.get("forwards", [])
                if isinstance(r, dict)
            ]
            return cls(
                proxy_port=data.get("proxy_port", 8080),
                traffic_archive_dir=data.get("traffic_archive_dir", "traffic_archive"),
                forward_mode=mode,
                user_token=str(data.get("user_token", "") or ""),
                forwards=forwards,
            )
        except (json.JSONDecodeError, TypeError):
            return cls()

    @property
    def forwarding_enabled(self) -> bool:
        """True when at least one rule is fully configured."""
        return any(r.enabled for r in self.forwards)

    @property
    def native_enabled(self) -> bool:
        """True when at least one enabled rule posts native Discord forwards."""
        return any(r.enabled and r.native for r in self.forwards)
