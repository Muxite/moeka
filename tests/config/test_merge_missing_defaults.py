"""Tests for recursive config default merging (moved from the removed onboard-wizard tests)."""

from nanobot.config.loader import merge_missing_defaults


class TestMergeMissingDefaults:
    """Tests for recursive config default merging."""

    def test_adds_missing_top_level_keys(self):
        existing = {"a": 1}
        defaults = {"a": 1, "b": 2, "c": 3}

        result = merge_missing_defaults(existing, defaults)

        assert result == {"a": 1, "b": 2, "c": 3}

    def test_preserves_existing_values(self):
        existing = {"a": "custom_value"}
        defaults = {"a": "default_value"}

        result = merge_missing_defaults(existing, defaults)

        assert result == {"a": "custom_value"}

    def test_merges_nested_dicts_recursively(self):
        existing = {
            "level1": {
                "level2": {
                    "existing": "kept",
                }
            }
        }
        defaults = {
            "level1": {
                "level2": {
                    "existing": "replaced",
                    "added": "new",
                },
                "level2b": "also_new",
            }
        }

        result = merge_missing_defaults(existing, defaults)

        assert result == {
            "level1": {
                "level2": {
                    "existing": "kept",
                    "added": "new",
                },
                "level2b": "also_new",
            }
        }

    def test_returns_existing_if_not_dict(self):
        assert merge_missing_defaults("string", {"a": 1}) == "string"
        assert merge_missing_defaults([1, 2, 3], {"a": 1}) == [1, 2, 3]
        assert merge_missing_defaults(None, {"a": 1}) is None
        assert merge_missing_defaults(42, {"a": 1}) == 42

    def test_returns_existing_if_defaults_not_dict(self):
        assert merge_missing_defaults({"a": 1}, "string") == {"a": 1}
        assert merge_missing_defaults({"a": 1}, None) == {"a": 1}

    def test_handles_empty_dicts(self):
        assert merge_missing_defaults({}, {"a": 1}) == {"a": 1}
        assert merge_missing_defaults({"a": 1}, {}) == {"a": 1}
        assert merge_missing_defaults({}, {}) == {}

    def test_backfills_channel_config(self):
        """Real-world scenario: backfill missing channel fields."""
        existing_channel = {
            "enabled": False,
            "appId": "",
            "secret": "",
        }
        default_channel = {
            "enabled": False,
            "appId": "",
            "secret": "",
            "msgFormat": "plain",
            "allowFrom": [],
        }

        result = merge_missing_defaults(existing_channel, default_channel)

        assert result["msgFormat"] == "plain"
        assert result["allowFrom"] == []

