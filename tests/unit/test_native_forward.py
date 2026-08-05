"""Tests for discordless.native_forward."""
from unittest.mock import MagicMock, patch

import pytest

from discordless.models import DiscordMessage
from discordless.native_forward import (
    _MIN_RATE_LIMIT_DELAY,
    NativeForwarder,
    resolve_token,
)

# Shape Discord tokens have: 24 . 6 . 27+ characters
FAKE_TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 27


@pytest.fixture
def forwarder():
    return NativeForwarder(
        token=FAKE_TOKEN,
        dest_channel_id="999999999999999999",
        rate_limit_delay=0.0,  # floored internally
    )


@pytest.fixture
def message():
    return DiscordMessage(
        channel_id="111111111111111111",
        author="testuser",
        content="Hello!",
        timestamp="2024-01-15T10:30:00.000Z",
        message_id="1234567890123456789",
        guild_id="222222222222222222",
    )


def _resp(status_code, payload=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = payload if payload is not None else {}
    mock.text = ""
    return mock


class TestForwardSuccess:
    def test_returns_created_ids_on_200(self, forwarder, message):
        resp = _resp(200, {"id": "555", "channel_id": "999999999999999999", "guild_id": "777"})
        with patch("discordless.native_forward.requests.post", return_value=resp):
            result = forwarder.forward_and_get_id(message)
        assert result == ("555", "999999999999999999", "777")
        assert forwarder.stats == {"sent": 1, "errors": 0}

    def test_forward_returns_true(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})):
            assert forwarder.forward(message) is True

    def test_posts_to_destination_channel(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder.forward(message)
        assert post.call_args.args[0] == (
            "https://discord.com/api/v9/channels/999999999999999999/messages"
        )


class TestForwardPayload:
    def test_uses_forward_reference_type(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder.forward(message)
        ref = post.call_args.kwargs["json"]["message_reference"]
        assert ref["type"] == 1  # 1 = forward, 0 would be a reply
        assert ref["channel_id"] == "111111111111111111"
        assert ref["message_id"] == "1234567890123456789"
        assert ref["guild_id"] == "222222222222222222"

    def test_sends_no_content_of_its_own(self, forwarder, message):
        """The snapshot carries the text — a caption would betray the automation."""
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder.forward(message)
        payload = post.call_args.kwargs["json"]
        assert payload["content"] == ""
        assert "username" not in payload
        assert "avatar_url" not in payload
        assert "embeds" not in payload

    def test_omits_guild_id_for_dm_source(self, forwarder):
        dm = DiscordMessage(
            channel_id="111",
            author="u",
            content="hi",
            timestamp="t",
            message_id="222",
        )
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder.forward(dm)
        assert "guild_id" not in post.call_args.kwargs["json"]["message_reference"]

    def test_sends_nonce(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder.forward(message)
        assert post.call_args.kwargs["json"]["nonce"].isdigit()

    def test_authorization_header_is_the_raw_token(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder.forward(message)
        headers = post.call_args.kwargs["headers"]
        assert headers["Authorization"] == FAKE_TOKEN  # no "Bot " prefix — user account
        assert "discord/" in headers["User-Agent"]


class TestForwardFailures:
    def test_missing_source_id_is_not_posted(self, forwarder):
        msg = DiscordMessage(
            channel_id="111", author="u", content="hi", timestamp="t", message_id=""
        )
        with patch("discordless.native_forward.requests.post") as post:
            result = forwarder.forward_and_get_id(msg)
        post.assert_not_called()
        assert result is None
        assert forwarder.stats["errors"] == 1

    def test_401_disables_the_forwarder(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(401)):
            assert forwarder.forward_and_get_id(message) is None
        assert forwarder.disabled is True
        # Subsequent calls short-circuit without touching the API
        with patch("discordless.native_forward.requests.post") as post:
            assert forwarder.forward_and_get_id(message) is None
        post.assert_not_called()

    def test_403_counts_an_error_but_keeps_running(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(403)):
            assert forwarder.forward_and_get_id(message) is None
        assert forwarder.disabled is False
        assert forwarder.stats["errors"] == 1

    def test_request_exception_returns_none(self, forwarder, message):
        import requests as req

        with patch("discordless.native_forward.requests.post", side_effect=req.RequestException):
            assert forwarder.forward_and_get_id(message) is None
        assert forwarder.stats["errors"] == 1

    def test_429_is_retried_then_succeeds(self, forwarder, message):
        responses = [_resp(429, {"retry_after": 0.1}), _resp(200, {"id": "42"})]
        with patch("discordless.native_forward.time.sleep"), patch(
            "discordless.native_forward.requests.post", side_effect=responses
        ) as post:
            result = forwarder.forward_and_get_id(message)
        assert post.call_count == 2
        assert result[0] == "42"
        assert forwarder.stats["sent"] == 1


class TestRateLimit:
    def test_delay_is_floored(self):
        assert NativeForwarder(FAKE_TOKEN, "1", rate_limit_delay=0.1).rate_limit_delay == (
            _MIN_RATE_LIMIT_DELAY
        )

    def test_higher_delay_is_kept(self):
        assert NativeForwarder(FAKE_TOKEN, "1", rate_limit_delay=3.0).rate_limit_delay == 3.0


class TestCapabilityFlags:
    def test_is_native(self, forwarder):
        assert forwarder.is_native is True

    def test_does_not_support_edits(self, forwarder):
        """Forwards are immutable snapshots — the addon must skip MESSAGE_UPDATE."""
        assert forwarder.supports_edits is False


class TestResolveToken:
    def test_explicit_token_wins(self):
        assert resolve_token("explicit.token.here") == "explicit.token.here"

    def test_explicit_token_is_stripped(self):
        assert resolve_token("  padded.token.value  ") == "padded.token.value"

    def test_reads_token_from_leveldb(self, tmp_path):
        store = tmp_path / "leveldb"
        store.mkdir()
        # \x00 delimiters mirror how the token sits between binary leveldb records
        (store / "000003.log").write_bytes(b"\x01junk\x00" + FAKE_TOKEN.encode() + b"\x00more\x02")
        with patch("discordless.native_forward._LEVELDB_GLOB", str(store / "*")):
            assert resolve_token() == FAKE_TOKEN

    def test_returns_empty_when_nothing_found(self, tmp_path):
        store = tmp_path / "leveldb"
        store.mkdir()
        (store / "000003.log").write_bytes(b"no token in here")
        with patch("discordless.native_forward._LEVELDB_GLOB", str(store / "*")):
            assert resolve_token() == ""

    def test_unreadable_store_is_skipped(self, tmp_path):
        with patch("discordless.native_forward._LEVELDB_GLOB", str(tmp_path / "missing" / "*")):
            assert resolve_token() == ""
