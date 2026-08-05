"""Shared pytest fixtures for Wirecord tests."""
import json
import sys
from pathlib import Path

import pytest

# Ensure project root is on the path so 'discordless' is importable
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_config_file(tmp_path):
    """Write a minimal valid config.json to a temp dir and return its path."""
    cfg = {
        "proxy_port": 8080,
        "traffic_archive_dir": str(tmp_path / "traffic_archive"),
        "forwards": [
            {
                "channels": ["111111111111111111"],
                "webhook_url": "https://discord.com/api/webhooks/test/token",
                "webhook_username": "TestBot",
                "rate_limit_delay": 0.0,
            }
        ],
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


@pytest.fixture
def native_config_file(tmp_path):
    """Write a config.json using global native forward mode."""
    cfg = {
        "proxy_port": 8080,
        "forward_mode": "native",
        "user_token": "test.token.value",
        "forwards": [
            {
                "channels": ["111111111111111111"],
                "webhook_url": "https://discord.com/api/webhooks/test/token",
                "webhook_channel_id": "999999999999999999",
            },
            {
                "channels": ["222222222222222222"],
                "webhook_url": "https://discord.com/api/webhooks/other/token",
                "forward_mode": "webhook",
            },
        ],
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


@pytest.fixture
def empty_config_file(tmp_path):
    """Write an empty (defaults-only) config.json."""
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# DiscordMessage fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_message_data():
    """Raw Discord API MESSAGE_CREATE 'd' payload."""
    return {
        "id": "1234567890123456789",
        "channel_id": "111111111111111111",
        "guild_id": "222222222222222222",
        "author": {"id": "333333333333333333", "username": "testuser"},
        "content": "Hello, World!",
        "timestamp": "2024-01-15T10:30:00.000Z",
        "edited_timestamp": None,
        "attachments": [],
        "embeds": [],
    }


@pytest.fixture
def sample_message(sample_message_data):
    """A DiscordMessage built from sample_message_data."""
    from discordless.models import DiscordMessage

    d = sample_message_data
    return DiscordMessage(
        channel_id=d["channel_id"],
        author=d["author"]["username"],
        content=d["content"],
        timestamp=d["timestamp"],
    )


# ---------------------------------------------------------------------------
# Gateway URL fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gateway_url_zlib():
    return "wss://gateway.discord.gg/?v=10&encoding=json&compress=zlib-stream"


@pytest.fixture
def gateway_url_json():
    return "wss://gateway.discord.gg/?v=10&encoding=json"
