"""Tests for discordless.config."""
import json

from discordless.config import MODE_NATIVE, MODE_WEBHOOK, Config, ForwardRule


class TestConfigLoad:
    def test_defaults_when_file_missing(self, tmp_path):
        cfg = Config.load(str(tmp_path / "nonexistent.json"))
        assert cfg.proxy_port == 8080
        assert cfg.forwards == []
        assert cfg.forward_mode == MODE_WEBHOOK
        assert cfg.forwarding_enabled is False

    def test_loads_values_from_file(self, minimal_config_file):
        cfg = Config.load(minimal_config_file)
        assert cfg.proxy_port == 8080
        assert len(cfg.forwards) == 1
        rule = cfg.forwards[0]
        assert rule.channels == ["111111111111111111"]
        assert rule.webhook_url == "https://discord.com/api/webhooks/test/token"
        assert rule.webhook_username == "TestBot"
        assert rule.rate_limit_delay == 0.0
        assert cfg.forwarding_enabled is True

    def test_strips_comment_keys(self, tmp_path):
        data = {"_comment": "ignored", "proxy_port": 9090}
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data))
        cfg = Config.load(str(p))
        assert cfg.proxy_port == 9090

    def test_ignores_unknown_keys(self, tmp_path):
        data = {"proxy_port": 1234, "unknown_key": "value"}
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data))
        cfg = Config.load(str(p))
        assert cfg.proxy_port == 1234

    def test_defaults_on_invalid_json(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("not valid json")
        cfg = Config.load(str(p))
        assert cfg.proxy_port == 8080


class TestForwardingEnabled:
    def test_disabled_when_no_webhook(self):
        cfg = Config(forwards=[ForwardRule(channels=["123"], webhook_url="")])
        assert cfg.forwarding_enabled is False

    def test_disabled_when_no_channels(self):
        cfg = Config(
            forwards=[ForwardRule(channels=[], webhook_url="https://example.com/webhook")]
        )
        assert cfg.forwarding_enabled is False

    def test_enabled_when_both_set(self):
        cfg = Config(
            forwards=[
                ForwardRule(channels=["123"], webhook_url="https://example.com/webhook")
            ]
        )
        assert cfg.forwarding_enabled is True


class TestForwardMode:
    def test_defaults_to_webhook(self):
        rule = ForwardRule.from_dict({"channels": ["1"], "webhook_url": "u"})
        assert rule.forward_mode == MODE_WEBHOOK
        assert rule.native is False

    def test_rule_inherits_global_mode(self, native_config_file):
        cfg = Config.load(native_config_file)
        assert cfg.forward_mode == MODE_NATIVE
        assert cfg.forwards[0].native is True

    def test_rule_can_override_global_mode(self, native_config_file):
        cfg = Config.load(native_config_file)
        # Second rule pins itself back to webhook despite the global native mode
        assert cfg.forwards[1].native is False

    def test_unknown_mode_falls_back_to_webhook(self, tmp_path):
        data = {"forward_mode": "carrier-pigeon", "forwards": [{"channels": ["1"], "webhook_url": "u"}]}
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data))
        cfg = Config.load(str(p))
        assert cfg.forward_mode == MODE_WEBHOOK
        assert cfg.forwards[0].native is False

    def test_native_enabled_reports_any_native_rule(self, native_config_file):
        cfg = Config.load(native_config_file)
        assert cfg.native_enabled is True

    def test_native_enabled_false_without_native_rules(self, minimal_config_file):
        cfg = Config.load(minimal_config_file)
        assert cfg.native_enabled is False

    def test_user_token_loaded(self, native_config_file):
        cfg = Config.load(native_config_file)
        assert cfg.user_token == "test.token.value"


class TestNativeRuleDestination:
    def test_falls_back_to_webhook_channel_id(self):
        rule = ForwardRule(
            channels=["1"],
            webhook_channel_id="999",
            forward_mode=MODE_NATIVE,
        )
        assert rule.destination == "999"
        assert rule.enabled is True

    def test_dest_channel_id_takes_precedence(self):
        rule = ForwardRule(
            channels=["1"],
            webhook_channel_id="999",
            dest_channel_id="888",
            forward_mode=MODE_NATIVE,
        )
        assert rule.destination == "888"

    def test_native_rule_needs_no_webhook_url(self):
        rule = ForwardRule(channels=["1"], dest_channel_id="888", forward_mode=MODE_NATIVE)
        assert rule.webhook_url == ""
        assert rule.enabled is True

    def test_native_rule_disabled_without_destination(self):
        rule = ForwardRule(
            channels=["1"],
            webhook_url="https://example.com/webhook",
            forward_mode=MODE_NATIVE,
        )
        assert rule.destination == ""
        assert rule.enabled is False
