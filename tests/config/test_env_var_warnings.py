"""Tests for the missing-env-var warning path in resolve_config_env_vars.

These pin the moeka deviation where the loader does *not* hard-fail on a
missing ``${VAR}``: instead it warns with the dotted field path so the
user can locate the unresolved reference in their config.json.
"""

from __future__ import annotations

import json

from nanobot.config.loader import load_config, resolve_config_env_vars


def _capture_warnings():
    """Return (records, handler_id) for a loguru WARNING-level sink."""
    from loguru import logger as loguru_logger

    records: list[str] = []
    handler_id = loguru_logger.add(lambda m: records.append(str(m)), level="WARNING")
    return records, handler_id


def _config_with_missing_var(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openrouter": {"apiKey": "${MISSING_TEST_VAR_FOR_WARNING}"},
                }
            }
        ),
        encoding="utf-8",
    )
    return load_config(config_path)


class TestEnvVarWarningPath:
    def test_warning_includes_var_name(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_TEST_VAR_FOR_WARNING", raising=False)
        raw = _config_with_missing_var(tmp_path)

        from loguru import logger as loguru_logger
        records, handler_id = _capture_warnings()
        try:
            resolve_config_env_vars(raw)
        finally:
            loguru_logger.remove(handler_id)

        env_warnings = [r for r in records if "MISSING_TEST_VAR_FOR_WARNING" in r]
        assert env_warnings, f"expected env-var warning, got: {records!r}"

    def test_warning_includes_field_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_TEST_VAR_FOR_WARNING", raising=False)
        raw = _config_with_missing_var(tmp_path)

        from loguru import logger as loguru_logger
        records, handler_id = _capture_warnings()
        try:
            resolve_config_env_vars(raw)
        finally:
            loguru_logger.remove(handler_id)

        # The warning must locate the missing var by config path so the user
        # can fix it without grepping. Field name uses Pydantic's Python attr
        # (``api_key``) since the model is what's walked.
        env_warnings = [r for r in records if "MISSING_TEST_VAR_FOR_WARNING" in r]
        joined = " ".join(env_warnings)
        assert "providers" in joined
        assert "openrouter" in joined
        assert "api_key" in joined

    def test_resolved_string_is_left_as_placeholder(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_TEST_VAR_FOR_WARNING", raising=False)
        raw = _config_with_missing_var(tmp_path)

        resolved = resolve_config_env_vars(raw)
        # Behavior contract: missing var leaves placeholder intact so the
        # rest of the system can still start.
        assert resolved.providers.openrouter.api_key == "${MISSING_TEST_VAR_FOR_WARNING}"

    def test_config_without_env_refs_returns_same_instance(self, tmp_path):
        """Identity-preservation: when nothing needs replacing, the resolver
        must return the same object so fields with ``exclude=True`` survive."""
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"providers": {"openrouter": {"apiKey": "literal-key"}}}),
            encoding="utf-8",
        )
        raw = load_config(config_path)
        resolved = resolve_config_env_vars(raw)
        assert resolved is raw

    def test_unset_var_does_not_warn_when_value_resolves(self, tmp_path, monkeypatch):
        """Sanity: warning fires only on a *missing* var, not on a resolved one."""
        monkeypatch.setenv("PRESENT_TEST_VAR", "hello")
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"providers": {"openrouter": {"apiKey": "${PRESENT_TEST_VAR}"}}}),
            encoding="utf-8",
        )
        raw = load_config(config_path)

        from loguru import logger as loguru_logger
        records, handler_id = _capture_warnings()
        try:
            resolved = resolve_config_env_vars(raw)
        finally:
            loguru_logger.remove(handler_id)

        assert resolved.providers.openrouter.api_key == "hello"
        env_warnings = [r for r in records if "PRESENT_TEST_VAR" in r]
        assert not env_warnings
