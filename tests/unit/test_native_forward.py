"""Tests for discordless.native_forward."""
import time
from unittest.mock import MagicMock, patch

import pytest

from discordless.models import DiscordMessage
from discordless.native_forward import (
    _MIN_RATE_LIMIT_DELAY,
    NativeForwarder,
    account_id,
    resolve_token,
    tokens_for_ids,
)

# Shape Discord tokens have: 24 . 6 . 27+ characters
FAKE_TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 27
SECOND_TOKEN = "D" * 24 + "." + "E" * 6 + "." + "F" * 27


@pytest.fixture
def forwarder():
    # start_worker=False → deterministic, synchronous testing of _send/_pick_*
    return NativeForwarder(
        accounts={"111": FAKE_TOKEN},
        dest_channel_id="999999999999999999",
        delay_min=0.0,  # floored to _MIN_RATE_LIMIT_DELAY
        start_worker=False,
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


class TestSendSuccess:
    def test_send_returns_created_id(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "555"})):
            assert forwarder._send(message) == "555"
        assert forwarder.stats["sent"] == 1
        assert forwarder.stats["errors"] == 0

    def test_posts_to_destination_channel(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder._send(message)
        assert post.call_args.args[0] == (
            "https://discord.com/api/v9/channels/999999999999999999/messages"
        )

    def test_uses_forward_reference_type(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder._send(message)
        ref = post.call_args.kwargs["json"]["message_reference"]
        assert ref["type"] == 1  # 1 = forward, 0 would be a reply
        assert ref["channel_id"] == "111111111111111111"
        assert ref["message_id"] == "1234567890123456789"
        assert ref["guild_id"] == "222222222222222222"

    def test_sends_no_content_of_its_own(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder._send(message)
        payload = post.call_args.kwargs["json"]
        assert payload["content"] == ""
        assert "username" not in payload and "embeds" not in payload

    def test_omits_guild_id_for_dm_source(self, forwarder):
        dm = DiscordMessage(channel_id="111", author="u", content="hi", timestamp="t", message_id="222")
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder._send(dm)
        assert "guild_id" not in post.call_args.kwargs["json"]["message_reference"]

    def test_authorization_header_is_the_raw_token(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "1"})) as post:
            forwarder._send(message)
        headers = post.call_args.kwargs["headers"]
        assert headers["Authorization"] == FAKE_TOKEN  # no "Bot " prefix — user account


class TestSendFailures:
    def test_401_disables_that_account(self, message):
        fwd = NativeForwarder({"111": FAKE_TOKEN, "222": SECOND_TOKEN}, "9", start_worker=False)
        with patch("discordless.native_forward.random.choice", return_value=("111", FAKE_TOKEN)), \
                patch("discordless.native_forward.requests.post", return_value=_resp(401)):
            assert fwd._send(message) is None
        assert "111" in fwd.disabled
        assert ("111", FAKE_TOKEN) not in fwd._live_accounts()
        assert ("222", SECOND_TOKEN) in fwd._live_accounts()

    def test_403_counts_error_keeps_account(self, forwarder, message):
        with patch("discordless.native_forward.requests.post", return_value=_resp(403)):
            assert forwarder._send(message) is None
        assert forwarder.disabled == set()
        assert forwarder.stats["errors"] == 1

    def test_request_exception_returns_none(self, forwarder, message):
        import requests as req
        with patch("discordless.native_forward.requests.post", side_effect=req.RequestException):
            assert forwarder._send(message) is None
        assert forwarder.stats["errors"] == 1

    def test_429_is_retried_then_succeeds(self, forwarder, message):
        responses = [_resp(429, {"retry_after": 0.1}), _resp(200, {"id": "42"})]
        with patch("discordless.native_forward.time.sleep"), \
                patch("discordless.native_forward.requests.post", side_effect=responses) as post:
            assert forwarder._send(message) == "42"
        assert post.call_count == 2
        assert forwarder.stats["sent"] == 1

    def test_no_live_account_is_an_error(self, forwarder, message):
        forwarder.disabled.add("111")
        with patch("discordless.native_forward.requests.post") as post:
            assert forwarder._send(message) is None
        post.assert_not_called()
        assert forwarder.stats["errors"] == 1


class TestPoolSelection:
    def test_pick_account_only_returns_live(self):
        fwd = NativeForwarder({"111": FAKE_TOKEN, "222": SECOND_TOKEN}, "9", start_worker=False)
        fwd.disabled.add("111")
        for _ in range(20):
            assert fwd._pick_account() == ("222", SECOND_TOKEN)

    def test_pick_account_none_when_all_disabled(self, forwarder):
        forwarder.disabled.add("111")
        assert forwarder._pick_account() is None

    def test_both_accounts_can_be_picked(self):
        fwd = NativeForwarder({"111": FAKE_TOKEN, "222": SECOND_TOKEN}, "9", start_worker=False)
        seen = {fwd._pick_account()[0] for _ in range(200)}
        assert seen == {"111", "222"}  # random draws hit both over 200 tries


class TestDelay:
    def test_delay_floored_to_minimum(self):
        fwd = NativeForwarder({"1": FAKE_TOKEN}, "9", delay_min=0.1, start_worker=False)
        assert fwd.delay_min == _MIN_RATE_LIMIT_DELAY
        assert fwd._pick_delay() == _MIN_RATE_LIMIT_DELAY  # min == max → fixed

    def test_delay_within_range(self):
        fwd = NativeForwarder({"1": FAKE_TOKEN}, "9", delay_min=2.0, delay_max=8.0, start_worker=False)
        for _ in range(200):
            d = fwd._pick_delay()
            assert 2.0 <= d <= 8.0

    def test_max_below_min_is_clamped(self):
        fwd = NativeForwarder({"1": FAKE_TOKEN}, "9", delay_min=5.0, delay_max=1.0, start_worker=False)
        assert fwd.delay_max == fwd.delay_min == 5.0


class TestEnqueueGuards:
    def test_missing_source_id_not_queued(self, forwarder):
        bad = DiscordMessage(channel_id="1", author="u", content="hi", timestamp="t", message_id="")
        forwarder.enqueue(bad)
        assert forwarder._q.qsize() == 0
        assert forwarder.stats["errors"] == 1

    def test_no_accounts_drops(self, message):
        fwd = NativeForwarder({}, "9", start_worker=False)
        fwd.enqueue(message)
        assert fwd.stats["dropped"] == 1


class TestWorkerIntegration:
    def test_enqueue_then_close_posts(self, message):
        fwd = NativeForwarder({"111": FAKE_TOKEN}, "9", delay_min=2.0, delay_max=8.0)
        fwd._pick_delay = lambda: 0.0  # no real waiting in the test
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "7"})) as post:
            fwd.enqueue(message)
            time.sleep(0.2)
            fwd.close(timeout=5)
        assert post.called
        assert fwd.stats["sent"] == 1

    def test_close_flushes_remaining_without_delay(self, message):
        # A long delay would normally block; close() must still flush the queue.
        fwd = NativeForwarder({"111": FAKE_TOKEN}, "9", delay_min=30.0, start_worker=False)
        fwd._q.put(message)
        with patch("discordless.native_forward.requests.post", return_value=_resp(200, {"id": "9"})) as post:
            fwd.close(timeout=1)
        assert post.called
        assert fwd.stats["sent"] == 1


class TestCapabilityFlags:
    def test_is_native(self, forwarder):
        assert forwarder.is_native is True

    def test_does_not_support_edits(self, forwarder):
        assert forwarder.supports_edits is False


class TestResolveToken:
    def test_explicit_token_wins(self):
        assert resolve_token("explicit.token.here") == "explicit.token.here"

    def test_explicit_token_is_stripped(self):
        assert resolve_token("  padded.token.value  ") == "padded.token.value"

    def test_explicit_token_wins_over_user_id(self):
        with patch("discordless.native_forward._collect_tokens") as collect:
            assert resolve_token("explicit.token", user_id="123") == "explicit.token"
        collect.assert_not_called()

    def test_reads_token_from_leveldb(self, tmp_path):
        store = tmp_path / "leveldb"
        store.mkdir()
        (store / "000003.log").write_bytes(b"\x01junk\x00" + FAKE_TOKEN.encode() + b"\x00more\x02")
        with patch("discordless.native_forward._LEVELDB_GLOB", str(store / "*")):
            assert resolve_token() == FAKE_TOKEN

    def test_returns_empty_when_nothing_found(self, tmp_path):
        store = tmp_path / "leveldb"
        store.mkdir()
        (store / "000003.log").write_bytes(b"no token in here")
        with patch("discordless.native_forward._LEVELDB_GLOB", str(store / "*")):
            assert resolve_token() == ""

    def test_first_token_wins_without_user_id(self):
        with patch("discordless.native_forward._collect_tokens", return_value=[FAKE_TOKEN, SECOND_TOKEN]):
            assert resolve_token() == FAKE_TOKEN

    def test_user_id_selects_matching_account(self):
        def fake_id(tok, *a, **k):
            return "111" if tok == FAKE_TOKEN else "222"

        with patch("discordless.native_forward._collect_tokens", return_value=[FAKE_TOKEN, SECOND_TOKEN]), \
                patch("discordless.native_forward.account_id", side_effect=fake_id):
            assert resolve_token(user_id="222") == SECOND_TOKEN

    def test_user_id_not_present_returns_empty(self):
        with patch("discordless.native_forward._collect_tokens", return_value=[FAKE_TOKEN, SECOND_TOKEN]), \
                patch("discordless.native_forward.account_id", return_value="999"):
            assert resolve_token(user_id="222") == ""


class TestTokensForIds:
    def test_maps_requested_ids(self):
        def fake_id(tok, *a, **k):
            return "111" if tok == FAKE_TOKEN else "222"

        with patch("discordless.native_forward._collect_tokens", return_value=[FAKE_TOKEN, SECOND_TOKEN]), \
                patch("discordless.native_forward.account_id", side_effect=fake_id):
            out = tokens_for_ids(["111", "222"])
        assert out == {"111": FAKE_TOKEN, "222": SECOND_TOKEN}

    def test_missing_id_absent_from_result(self):
        with patch("discordless.native_forward._collect_tokens", return_value=[FAKE_TOKEN]), \
                patch("discordless.native_forward.account_id", return_value="111"):
            out = tokens_for_ids(["111", "999"])
        assert out == {"111": FAKE_TOKEN}

    def test_explicit_token_takes_precedence(self):
        # An explicitly-pinned token for an id skips the leveldb lookup for it
        with patch("discordless.native_forward._collect_tokens", return_value=[]) as collect:
            out = tokens_for_ids(["111"], explicit_by_id={"111": "pinned.tok.value"})
        assert out == {"111": "pinned.tok.value"}
        collect.assert_not_called()

    def test_empty_ids_returns_empty(self):
        assert tokens_for_ids([]) == {}


class TestAccountId:
    def test_returns_id_on_200(self):
        with patch("discordless.native_forward.requests.get", return_value=_resp(200, {"id": "42"})):
            assert account_id(FAKE_TOKEN) == "42"

    def test_returns_empty_on_401(self):
        with patch("discordless.native_forward.requests.get", return_value=_resp(401)):
            assert account_id(FAKE_TOKEN) == ""

    def test_returns_empty_on_network_error(self):
        import requests as req
        with patch("discordless.native_forward.requests.get", side_effect=req.RequestException):
            assert account_id(FAKE_TOKEN) == ""
