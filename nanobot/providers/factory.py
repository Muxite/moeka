"""Create LLM providers from config."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.config.schema import Config, InlineFallbackConfig, ModelPresetConfig, ProviderConfig
from nanobot.providers.base import GenerationSettings, LLMProvider, resolve_stream_idle_timeout_s
from nanobot.providers.fallback_provider import FallbackProvider
from nanobot.providers.registry import ProviderSpec, create_dynamic_spec, find_by_name

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.ledger import ModelPricing, PricingKey, PricingTable


@dataclass(frozen=True)
class ProviderSnapshot:
    provider: LLMProvider
    model: str
    context_window_tokens: int
    signature: tuple[object, ...]
    generation: GenerationSettings | None = None
    model_preset: str | None = None


@dataclass(frozen=True)
class _ProviderSetup:
    model: str
    provider_name: str
    provider_config: ProviderConfig | None
    spec: ProviderSpec | None
    backend: str


def _provider_api_key(
    provider_name: str,
    provider_config: ProviderConfig | None,
    env: CoreEnvironment | None,
) -> str | None:
    """Configured key, else ``providers/<slot>/api_key`` from the host resolver (I1).

    ``env=None`` keeps the pre-kernel behaviour: the configured key only. The
    config passed in is authoritative for config keys, so a resolver that mirrors
    config (the legacy adapter) is asked for non-config refs only: a key removed
    from config on hot reload is not resurrected from the startup copy.
    """
    key = provider_config.api_key if provider_config else None
    if key or env is None:
        return key
    from nanobot.kernel.env import resolve_credential

    return resolve_credential(
        env,
        f"providers/{provider_name}/api_key",
        f"provider:{provider_name}",
        exclude_config=True,
    )


def credential_predicate(env: CoreEnvironment | None) -> Callable[[str], bool] | None:
    """``has_credential`` for :meth:`Config.match_provider` from the host resolver.

    ``None`` without an env (config-only matching). With the legacy adapter every
    provider ref is config-backed, so the predicate adds nothing and matching is
    exactly the config-only rule; a kernel host's resolver-only keys count.
    """
    if env is None:
        return None

    def _has(slot: str) -> bool:
        return bool(_provider_api_key(slot, None, env))

    return _has


def _runtime_settings(env: CoreEnvironment | None) -> dict[str, object]:
    """The host ``runtime`` section; ``env=None`` = legacy process-env settings."""
    if env is not None:
        return dict(env.config.section("runtime"))
    from nanobot.kernel.legacy import legacy_runtime_settings

    return dict(legacy_runtime_settings())


def _stream_idle_timeout_setting(env: CoreEnvironment | None) -> float:
    return resolve_stream_idle_timeout_s(
        env_value=_runtime_settings(env).get("stream_idle_timeout_s")
    )


def _apply_runtime_settings(provider: LLMProvider, env: CoreEnvironment | None) -> None:
    """Set provider tunables from the host env (defaults when the host sets none).

    Providers that bake a timeout into a client at construction (Bedrock) also
    receive it as a constructor argument; this keeps the attribute consistent.
    """
    runtime = _runtime_settings(env)
    provider.stream_idle_timeout_s = resolve_stream_idle_timeout_s(
        env_value=runtime.get("stream_idle_timeout_s")
    )
    if hasattr(provider, "request_timeout_s"):
        from nanobot.providers.openai_compat_provider import resolve_openai_compat_timeout_s

        provider.request_timeout_s = resolve_openai_compat_timeout_s(
            runtime.get("openai_compat_timeout_s")
        )
    if env is not None:
        # Request-extras drops (``sampling.dropped``) go to the host's trace sink.
        provider.trace_sink = env.trace


def _resolve_model_preset(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ModelPresetConfig:
    return preset if preset is not None else config.resolve_preset(preset_name)


def _provider_extra_headers(
    spec: ProviderSpec | None,
    provider_config: ProviderConfig | None,
) -> dict[str, str] | None:
    headers = dict(spec.default_extra_headers) if spec else {}
    if provider_config and provider_config.extra_headers:
        headers.update(provider_config.extra_headers)
    return headers or None


def _provider_spec_for_config(
    provider_name: str,
    provider_config: ProviderConfig | None,
) -> ProviderSpec | None:
    spec = find_by_name(provider_name)
    if (
        spec is not None
        and spec.name == "orcarouter"
        and provider_config is not None
        and provider_config.api_base
        and provider_config.api_base.rstrip("/").lower()
        != spec.default_api_base.rstrip("/").lower()
    ):
        # Before OrcaRouter became a built-in provider, this name was valid for a
        # dynamic custom provider. Preserve that provider's model-prefix behavior
        # when an existing config points the name at a different endpoint.
        return create_dynamic_spec(
            provider_name,
            display_name=provider_config.display_name or "",
            thinking_style=provider_config.thinking_style or "",
        )
    return spec


def _resolve_provider_setup(
    config: Config,
    *,
    preset: ModelPresetConfig,
    model: str | None = None,
    env: CoreEnvironment | None = None,
) -> _ProviderSetup:
    """Resolve and validate provider configuration without constructing a client."""
    model = model or preset.model
    has_credential = credential_predicate(env)
    p, provider_name = config.match_provider(model, preset=preset, has_credential=has_credential)
    if not provider_name:
        raise ValueError(f"No provider is configured for model '{model}'.")
    spec = _provider_spec_for_config(provider_name, p)
    if not spec and p:
        if not p.api_base:
            raise ValueError(f"Provider '{provider_name}' requires api_base in config.")
        spec = create_dynamic_spec(
            provider_name,
            display_name=(p.display_name or "") if p else "",
            thinking_style=(p.thinking_style or "") if p else "",
        )
    if spec and spec.is_transcription_only:
        raise ValueError(f"Provider '{provider_name}' only supports transcription.")
    backend = spec.backend if spec else "openai_compat"
    if p and p.proxy and backend not in {"openai_compat", "openai_codex", "xai_grok"}:
        raise ValueError(
            f"providers.{provider_name}.proxy is only supported for "
            "OpenAI-compatible providers, OpenAI Codex, and xAI Grok."
        )

    if backend == "azure_openai":
        if not p or not p.api_base:
            raise ValueError("Azure OpenAI requires api_base in config.")
    elif (
        backend == "openai_compat"
        and spec
        and spec.is_direct
        and not spec.default_api_base
        and not (p and p.api_base)
    ):
        raise ValueError(f"Provider '{provider_name}' requires api_base in config.")
    elif backend in {"anthropic", "openai_compat"} and not (
        backend == "openai_compat" and model.startswith("bedrock/")
    ):
        needs_key = not _provider_api_key(provider_name, p, env)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            raise ValueError(f"No API key configured for provider '{provider_name}'.")

    return _ProviderSetup(
        model=model,
        provider_name=provider_name,
        provider_config=p,
        spec=spec,
        backend=backend,
    )


def validate_provider_setup(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
    env: CoreEnvironment | None = None,
) -> None:
    """Validate local provider/model settings without loading a provider client."""
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    _resolve_provider_setup(
        config,
        preset=resolved,
        model=model,
        env=env,
    )


def _make_provider_core(
    config: Config,
    *,
    preset: ModelPresetConfig,
    model: str | None = None,
    data_dir: Path | None = None,
    env: CoreEnvironment | None = None,
) -> LLMProvider:
    """Create a plain LLM provider without failover wrapping."""
    setup = _resolve_provider_setup(
        config,
        preset=preset,
        model=model,
        env=env,
    )
    model = setup.model
    provider_name = setup.provider_name
    p = setup.provider_config
    spec = setup.spec
    backend = setup.backend
    api_key = _provider_api_key(provider_name, p, env)

    if backend == "openai_codex":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        provider = OpenAICodexProvider(
            default_model=model,
            proxy=getattr(p, "proxy", None) if p else None,
            extra_body=p.extra_body if p else None,
            provider_name=provider_name,
        )
    elif backend == "xai_grok":
        from nanobot.providers.xai_grok_provider import XAIGrokProvider

        provider = XAIGrokProvider(
            default_model=model,
            proxy=getattr(p, "proxy", None) if p else None,
            extra_body=p.extra_body if p else None,
            provider_name=provider_name,
            data_dir=data_dir,
        )
    elif backend == "azure_openai":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

        if p is None or p.api_base is None:
            raise RuntimeError("validated Azure provider setup is missing api_base")
        provider = AzureOpenAIProvider(
            api_key=api_key or "",
            api_base=p.api_base,
            default_model=model,
            provider_name=provider_name,
        )
    elif backend == "github_copilot":
        from nanobot.providers.github_copilot_provider import GitHubCopilotProvider

        provider = GitHubCopilotProvider(
            default_model=model, provider_name=provider_name, env=env,
        )
    elif backend == "anthropic":
        from nanobot.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=api_key,
            api_base=config.get_api_base(
                model, preset=preset, has_credential=credential_predicate(env)
            ),
            default_model=model,
            extra_headers=_provider_extra_headers(spec, p),
            provider_name=provider_name,
        )
    elif backend == "bedrock":
        from nanobot.providers.bedrock_provider import BedrockProvider

        provider = BedrockProvider(
            api_key=api_key,
            api_base=p.api_base if p else None,
            default_model=model,
            region=getattr(p, "region", None) if p else None,
            profile=getattr(p, "profile", None) if p else None,
            extra_body=p.extra_body if p else None,
            provider_name=provider_name,
            env=env,
            stream_idle_timeout_s=_stream_idle_timeout_setting(env),
        )
    else:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=api_key,
            api_base=config.get_api_base(
                model, preset=preset, has_credential=credential_predicate(env)
            ),
            default_model=model,
            extra_headers=_provider_extra_headers(spec, p),
            spec=spec,
            extra_body=p.extra_body if p else None,
            api_type=p.api_type if p and provider_name == "openai" else "auto",
            extra_query=p.extra_query if p else None,
            proxy=p.proxy if p else None,
            provider_name=provider_name,
            env=env,
        )

    provider.generation = preset.to_generation_settings()
    _apply_runtime_settings(provider, env)
    return provider


def _inline_fallback_preset(
    primary: ModelPresetConfig,
    fallback: InlineFallbackConfig,
) -> ModelPresetConfig:
    return ModelPresetConfig(
        model=fallback.model,
        provider=fallback.provider,
        max_tokens=fallback.max_tokens if fallback.max_tokens is not None else primary.max_tokens,
        context_window_tokens=(
            fallback.context_window_tokens
            if fallback.context_window_tokens is not None
            else primary.context_window_tokens
        ),
        temperature=(
            fallback.temperature if fallback.temperature is not None else primary.temperature
        ),
        reasoning_effort=fallback.reasoning_effort,
    )


def _resolve_fallback_presets(config: Config, primary: ModelPresetConfig) -> list[ModelPresetConfig]:
    presets: list[ModelPresetConfig] = []
    for fallback in config.agents.defaults.fallback_models:
        if isinstance(fallback, str):
            presets.append(config.model_presets[fallback])
        else:
            presets.append(_inline_fallback_preset(primary, fallback))
    return presets


def make_provider(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
    data_dir: Path | None = None,
    env: CoreEnvironment | None = None,
) -> LLMProvider:
    """Create the LLM provider implied by config.

    When *model* is given, it overrides the resolved/preset model — used by
    the failover path to create providers for fallback models. *data_dir* is the
    host's instance data dir for OAuth token stores; it defaults to
    ``env.paths.data_dir`` when *env* is given, and ``None`` without either keeps
    the legacy ``get_data_dir()``. *env* supplies credentials a config slot lacks
    (``providers/<slot>/api_key`` under scope ``provider:<slot>``); ``None`` keeps
    today's config-only keys.
    """
    if data_dir is None and env is not None:
        data_dir = env.paths.data_dir
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    provider = _make_provider_core(
        config, preset=resolved, model=model, data_dir=data_dir, env=env,
    )
    fallback_presets = _resolve_fallback_presets(config, resolved)

    if fallback_presets:
        provider = FallbackProvider(
            primary=provider,
            fallback_presets=fallback_presets,
            provider_factory=lambda fb: _make_provider_core(
                config, preset=fb, data_dir=data_dir, env=env,
            ),
            primary_context_window_tokens=resolved.context_window_tokens,
        )

    if env is not None:
        attach_ledger(provider, env, _ledger_pricing(config, resolved, fallback_presets, env))
    return provider


def _ledger_pricing(
    config: Config,
    resolved: ModelPresetConfig,
    fallback_presets: list[ModelPresetConfig],
    env: CoreEnvironment | None = None,
) -> PricingTable:
    """``(provider, model)`` -> ``ModelPricing`` for every preset that sets a tier or a price.

    Ruling J: each preset is keyed by the provider name ``Config.match_provider``
    resolves for it (the same name the built provider reports on its call records),
    so two presets sharing a model string under different providers never swap tier
    or price. A preset whose provider cannot be resolved falls back to a bare-model
    wildcard key. The active preset wins over its fallbacks, which win over other
    named presets; a conflicting duplicate is logged as a collision.
    """
    from nanobot.kernel.ledger import ModelPricing, PricingTable

    has_credential = credential_predicate(env)
    table = PricingTable()
    seen: set[int] = set()
    for preset in (resolved, *fallback_presets, *config.model_presets.values()):
        if id(preset) in seen:
            continue
        seen.add(id(preset))
        values = (
            preset.tier,
            preset.price_in_per_mtok,
            preset.price_out_per_mtok,
            preset.price_cache_read_per_mtok,
        )
        if all(value is None for value in values):
            continue
        try:
            _, provider_name = config.match_provider(
                preset.model, preset=preset, has_credential=has_credential,
            )
        except Exception:  # noqa: BLE001 - an unresolvable preset still gets a wildcard
            provider_name = None
        key = (provider_name, preset.model) if provider_name else preset.model
        table.add(key, ModelPricing(*values))
    return table


def attach_ledger(
    provider: LLMProvider,
    env: CoreEnvironment,
    pricing: PricingTable | Mapping[PricingKey, ModelPricing] | None = None,
) -> None:
    """The single ledger wiring point: every call through *provider* is measured.

    Emits ``model.call`` to ``env.trace`` and records to the ``LLMUsageStore`` under
    ``env.paths.data_dir``. Used by :func:`make_provider` and for host-injected
    providers (``LLM.register_provider``). *pricing* is a :class:`PricingTable` or
    the mapping one is built from; ``None`` prices nothing (cost unknown).
    Fail-open: wiring problems are logged, never raised.
    """
    try:
        from nanobot.kernel.ledger import LedgerObserver
        from nanobot.llm_usage import get_llm_usage_store

        provider.set_llm_call_observer(LedgerObserver(
            sink=env.trace,
            store=get_llm_usage_store(data_dir=env.paths.data_dir),
            pricing=pricing,
        ))
    except Exception:
        from loguru import logger

        logger.exception("failed to attach the cost ledger to {}", provider.provider_name)


def _attach_ledger(
    provider: LLMProvider,
    config: Config,
    resolved: ModelPresetConfig,
    fallback_presets: list[ModelPresetConfig],
    env: CoreEnvironment,
) -> None:
    """Deprecated private spelling of :func:`attach_ledger` (prices from the config)."""
    attach_ledger(provider, env, _ledger_pricing(config, resolved, fallback_presets, env))


def build_unconfigured_provider_snapshot(config: Config, setup_error: str) -> ProviderSnapshot:
    """Build a non-networking runtime so the WebUI can collect first-time setup."""
    from nanobot.providers.unconfigured_provider import UnconfiguredProvider

    preset = config.resolve_preset()
    provider = UnconfiguredProvider(preset.model)
    provider.generation = preset.to_generation_settings()
    return ProviderSnapshot(
        provider=provider,
        model=preset.model,
        context_window_tokens=preset.context_window_tokens,
        signature=("unconfigured", setup_error, preset.model),
        generation=provider.generation,
    )


def provider_signature(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    env: CoreEnvironment | None = None,
) -> tuple[object, ...]:
    """Return the config fields that affect the active provider chain.

    *env* makes provider matching resolver-aware exactly as :func:`make_provider`
    does, so the signature names the provider that is actually built.
    """
    has = credential_predicate(env)
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    p = config.get_provider(resolved.model, preset=resolved, has_credential=has)
    fallback_presets = _resolve_fallback_presets(config, resolved)

    def _fallback_signature(fallback: ModelPresetConfig) -> tuple[object, ...]:
        fp, provider_name = config.match_provider(
            fallback.model, preset=fallback, has_credential=has
        )
        return (
            fallback.model,
            fallback.provider,
            provider_name,
            fp.api_key if fp else None,
            config.get_api_base(fallback.model, preset=fallback, has_credential=has),
            _provider_extra_headers(find_by_name(provider_name) if provider_name else None, fp),
            fp.extra_body if fp else None,
            fp.api_type if fp else "auto",
            fp.extra_query if fp else None,
            getattr(fp, "region", None) if fp else None,
            getattr(fp, "profile", None) if fp else None,
            fallback.max_tokens,
            fallback.temperature,
            fallback.reasoning_effort,
            fallback.context_window_tokens,
            getattr(fp, "proxy", None) if fp else None,
            fp.thinking_style if fp else None,
        )

    provider_name = config.get_provider_name(resolved.model, preset=resolved, has_credential=has)
    return (
        resolved.model,
        resolved.provider,
        provider_name,
        p.api_key if p else None,
        config.get_api_base(resolved.model, preset=resolved, has_credential=has),
        _provider_extra_headers(find_by_name(provider_name) if provider_name else None, p),
        p.extra_body if p else None,
        p.api_type if p else "auto",
        p.extra_query if p else None,
        getattr(p, "region", None) if p else None,
        getattr(p, "profile", None) if p else None,
        resolved.max_tokens,
        resolved.temperature,
        resolved.reasoning_effort,
        resolved.context_window_tokens,
        getattr(p, "proxy", None) if p else None,
        p.thinking_style if p else None,
        tuple(_fallback_signature(fallback) for fallback in fallback_presets),
    )


def build_provider_snapshot(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    env: CoreEnvironment | None = None,
) -> ProviderSnapshot:
    """Build a provider snapshot; *env* threads credentials and ``data_dir`` (see make_provider)."""
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    selected_preset = (
        config.agents.defaults.model_preset
        if preset_name is None and preset is None
        else preset_name
    )
    fallback_windows = [
        fallback.context_window_tokens
        for fallback in _resolve_fallback_presets(config, resolved)
    ]
    return ProviderSnapshot(
        provider=make_provider(config, preset=resolved, env=env),
        model=resolved.model,
        context_window_tokens=min([resolved.context_window_tokens, *fallback_windows]),
        signature=provider_signature(config, preset=resolved, env=env),
        generation=resolved.to_generation_settings(),
        model_preset=selected_preset,
    )


def load_provider_snapshot(
    config_path: Path | None = None,
    *,
    preset_name: str | None = None,
    env: CoreEnvironment | None = None,
) -> ProviderSnapshot:
    """Build a snapshot from the current config (a host ``provider_snapshot_loader``).

    With *env* (and no *config_path*) the config comes from ``env.config``; the
    legacy adapter re-reads ``config.json`` when it changed. Otherwise the config
    file at *config_path* (``None`` = the current config path) is read through the
    legacy adapter. *env* always supplies credentials and ``data_dir``.
    """
    from nanobot.config.loader import snapshot_config

    if env is not None and config_path is None:
        source = env.config
    else:
        from nanobot.kernel.legacy import file_config_source

        source = file_config_source(config_path)
    return build_provider_snapshot(snapshot_config(source), preset_name=preset_name, env=env)
