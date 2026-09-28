"""Host environment builder: the one way a host describes itself to a ``Kernel``.

``Environment.for_host`` turns explicit host inputs (paths, credentials,
provider endpoints, model specs) into an in-memory ``Config`` plus a
``CoreEnvironment``. Nothing is read from the process environment, ``$HOME``,
the current directory or ``config.json`` (I1), and no directory is created:
the host owns ``work_dir`` and ``state_dir`` and the kernel creates what it
needs under them lazily, on first use.

API keys never enter the ``Config``: each provider asks the credential
resolver for ``providers/<name>/api_key`` (scope ``provider:<name>``), and
``ProviderSpec.credential`` aliases that ref to the host's own ref name.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from nanobot.kernel.env import (
    CoreEnvironment,
    CredentialResolver,
    Paths,
    StaticCredentialResolver,
)
from nanobot.kernel.frozen import FrozenMap, thaw
from nanobot.kernel.sampling import Sampling
from nanobot.kernel.trace import NullTraceSink, TraceSink

if TYPE_CHECKING:
    from nanobot.config.schema import Config


@dataclass(frozen=True)
class ProviderSpec:
    """One provider endpoint (``providers.<name>`` in the legacy config).

    ``name`` is a registry provider name (``openai``, ``anthropic``, ``vllm``, ...)
    or any other name for a custom OpenAI-compatible endpoint (``api_base``
    required then). ``credential`` is the credential-resolver ref holding the API
    key; ``None`` means the resolver is asked for ``providers/<name>/api_key``.

    Hashable: ``extra_headers`` / ``extra_body`` accept any mapping and are stored
    as :class:`~nanobot.kernel.frozen.FrozenMap` (``.as_dict()`` for a plain copy).
    """

    name: str
    api_base: str | None = None
    credential: str | None = None
    extra_headers: Mapping[str, str] = field(default_factory=FrozenMap)
    extra_body: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        for name in ("extra_headers", "extra_body"):
            value = getattr(self, name)
            if not isinstance(value, FrozenMap):
                object.__setattr__(self, name, FrozenMap(value))


@dataclass(frozen=True)
class ModelSpec:
    """A named model on one provider, with its limits, defaults and prices.

    Prices are USD per million tokens; absent = unknown (never free), except
    ``tier="local"`` with no prices, which is the cost-0 convention.
    ``native_json`` records whether the endpoint honours ``response_format``
    natively (``None`` = unknown); it is host metadata, not config.
    """

    name: str
    model: str
    provider: str
    tier: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    sampling: Sampling | None = None
    price_in: float | None = None
    price_out: float | None = None
    price_cache_read: float | None = None
    native_json: bool | None = None


def _provider_ref(name: str) -> str:
    return f"providers/{name}/api_key"


class _AliasedCredentialResolver:
    """Answers ``providers/<name>/api_key`` from the host ref a ``ProviderSpec`` names.

    Every other ref goes to the host resolver unchanged, so a host may also
    serve ``providers/<name>/api_key`` (or any tool ref) directly.
    """

    def __init__(self, inner: CredentialResolver, aliases: Mapping[str, str]) -> None:
        self._inner = inner
        self._aliases = dict(aliases)

    def resolve(self, ref: str, scope: str) -> str | None:
        alias = self._aliases.get(ref)
        if alias is not None:
            value = self._inner.resolve(alias, scope)
            if value:
                return value
        return self._inner.resolve(ref, scope)

    def __repr__(self) -> str:
        return f"AliasedCredentialResolver({self._inner!r}, aliases={sorted(self._aliases)})"


class _InMemoryConfigSource:
    """``ConfigSource`` over a held ``Config``; ``snapshot()`` hands back that object."""

    def __init__(self, config: Config, runtime: Mapping[str, Any] | None = None) -> None:
        self._config = config
        self._runtime = dict(runtime or {})

    def snapshot(self) -> Config:
        return self._config

    def section(self, name: str) -> dict[str, Any]:
        if name == "runtime":
            return dict(self._runtime)
        if name not in type(self._config).model_fields:
            return {}
        value = self._config.model_dump(by_alias=False, include={name}).get(name)
        return value if isinstance(value, dict) else {}

    def __repr__(self) -> str:
        return "InMemoryConfigSource()"


def _validate_config(data: Mapping[str, Any]) -> Config:
    """Validate a ``Config`` from explicit data only: no ``${VAR}`` expansion and no
    settings-env read.

    ``Config`` is a pydantic ``BaseSettings`` (``env_prefix="NANOBOT_"``): its
    ``__init__`` (which ``model_validate`` runs) scans the whole process
    environment and lets ``NANOBOT_*`` variables override fields. So each
    top-level section is validated on its own and assembled with
    ``model_construct``, then the cross-section checks run explicitly.
    """
    from pydantic import TypeAdapter

    from nanobot.config.schema import Config, _resolve_tool_config_refs

    try:
        _resolve_tool_config_refs()
    except Exception:  # noqa: BLE001 - same tolerance as config_from_sources
        pass
    by_key: dict[str, str] = {}
    for name, info in Config.model_fields.items():
        by_key[name] = name
        if info.alias:
            by_key[info.alias] = name
        choices = getattr(info.validation_alias, "choices", None) or ()
        for choice in choices:
            if isinstance(choice, str):
                by_key[choice] = name
    values: dict[str, Any] = {}
    for key, value in data.items():
        name = by_key.get(key)
        if name is None:
            raise ValueError(f"unknown config section {key!r}")
        annotation = Config.model_fields[name].annotation
        values[name] = TypeAdapter(annotation).validate_python(value)
    config = Config.model_construct(**values)
    config._validate_model_preset()
    return config


def _preset_dict(spec: ModelSpec) -> dict[str, Any]:
    preset: dict[str, Any] = {"model": spec.model, "provider": spec.provider}
    if spec.max_tokens is not None:
        preset["max_tokens"] = spec.max_tokens
    if spec.context_window is not None:
        preset["context_window_tokens"] = spec.context_window
    if spec.tier is not None:
        preset["tier"] = spec.tier
    if spec.price_in is not None:
        preset["price_in_per_mtok"] = spec.price_in
    if spec.price_out is not None:
        preset["price_out_per_mtok"] = spec.price_out
    if spec.price_cache_read is not None:
        preset["price_cache_read_per_mtok"] = spec.price_cache_read
    sampling = spec.sampling
    if sampling is not None:
        if sampling.temperature is not None:
            preset["temperature"] = sampling.temperature
        if sampling.reasoning_effort is not None:
            preset["reasoning_effort"] = sampling.reasoning_effort
    return preset


def _make_paths(work_dir: Path | str, state_dir: Path | str, strict: bool) -> Paths:
    # Strict: Paths itself raises PathsOverlapError on overlap (I2). Non-strict
    # hosts may share one directory (the legacy flat layout).
    return Paths(work_dir=Path(work_dir), state_dir=Path(state_dir), overlap_ok=not strict)


class Environment:
    """What a host hands to ``Kernel``: config, credentials, paths and trace.

    Build it with :meth:`for_host` (kernel-native hosts) or :meth:`from_config`
    (legacy ``Config`` escape hatch). Construction performs no filesystem writes.
    """

    def __init__(
        self,
        *,
        core: CoreEnvironment,
        config: Config,
        models: Mapping[str, ModelSpec],
        default_model: str,
        offline: bool = False,
    ) -> None:
        self._core = core
        self._config = config
        self._models = MappingProxyType(dict(models))
        self._default_model = default_model
        self._offline = offline

    # -- constructors ----------------------------------------------------

    @classmethod
    def for_host(
        cls,
        *,
        state_dir: Path | str,
        work_dir: Path | str,
        credentials: Mapping[str, str] | CredentialResolver,
        providers: Iterable[ProviderSpec],
        models: Iterable[ModelSpec],
        default_model: str,
        trace: TraceSink | None = None,
        tools: Mapping[str, Any] | None = None,
        exec_base_env: Mapping[str, str] | None = None,
        strict: bool = True,
        offline: bool = False,
    ) -> Environment:
        """Build an environment from explicit host inputs only (zero ambient reads).

        Raises ``ValueError`` for duplicate names, a ``ModelSpec`` naming an
        unknown provider, or a ``default_model`` that names no ``ModelSpec``;
        ``PathsOverlapError`` (a ``ValueError``) when ``strict`` and the two
        directories overlap.
        """
        provider_list = list(providers)
        model_list = list(models)
        provider_names = [p.name for p in provider_list]
        if len(set(provider_names)) != len(provider_names):
            raise ValueError(f"duplicate ProviderSpec names: {provider_names}")
        model_names = [m.name for m in model_list]
        if len(set(model_names)) != len(model_names):
            raise ValueError(f"duplicate ModelSpec names: {model_names}")
        if "default" in model_names:
            raise ValueError("ModelSpec name 'default' is reserved")
        known = set(provider_names)
        for spec in model_list:
            if spec.provider not in known:
                raise ValueError(
                    f"ModelSpec {spec.name!r} names unknown provider {spec.provider!r}; "
                    f"known providers: {sorted(known)}"
                )
        by_name = {m.name: m for m in model_list}
        if default_model not in by_name:
            raise ValueError(
                f"default_model {default_model!r} names no ModelSpec; "
                f"known models: {sorted(by_name)}"
            )

        paths = _make_paths(work_dir, state_dir, strict)
        default = by_name[default_model]
        defaults: dict[str, Any] = {
            "workspace": str(paths.work_dir),
            "model_preset": default_model,
            # Mirror the default preset into the flat fields for code paths that
            # read agents.defaults directly instead of resolving the preset.
            "model": default.model,
            "provider": default.provider,
        }
        default_preset = _preset_dict(default)
        for key in ("max_tokens", "context_window_tokens", "temperature", "reasoning_effort"):
            if key in default_preset:
                defaults[key] = default_preset[key]

        provider_section: dict[str, Any] = {}
        for p in provider_list:
            entry: dict[str, Any] = {}
            if p.api_base is not None:
                entry["api_base"] = p.api_base
            if p.extra_headers:
                entry["extra_headers"] = thaw(p.extra_headers)
            if p.extra_body:
                entry["extra_body"] = thaw(p.extra_body)
            provider_section[p.name] = entry

        data: dict[str, Any] = {
            "agents": {"defaults": defaults},
            "providers": provider_section,
            "model_presets": {m.name: _preset_dict(m) for m in model_list},
        }
        if tools:
            data["tools"] = dict(tools)
        config = _validate_config(data)

        aliases = {_provider_ref(p.name): p.credential for p in provider_list if p.credential}
        if isinstance(credentials, Mapping):
            # Keys a ProviderSpec names are readable only by that provider's scope.
            scopes: dict[str, set[str]] = {}
            for p in provider_list:
                if p.credential:
                    scopes.setdefault(p.credential, set()).add(f"provider:{p.name}")
            resolver: CredentialResolver = StaticCredentialResolver(credentials, scopes=scopes)
        else:
            resolver = credentials
        if aliases:
            resolver = _AliasedCredentialResolver(resolver, aliases)

        core = CoreEnvironment(
            config=_InMemoryConfigSource(config),
            credentials=resolver,
            paths=paths,
            trace=trace if trace is not None else NullTraceSink(),
            exec_base_env=dict(exec_base_env or {}),
            strict=strict,
        )
        return cls(
            core=core, config=config, models=by_name, default_model=default_model,
            offline=offline,
        )

    @classmethod
    def from_config(
        cls,
        config: Config | Mapping[str, Any],
        *,
        state_dir: Path | str,
        work_dir: Path | str,
        trace: TraceSink | None = None,
        strict: bool = False,
        credentials: Mapping[str, str] | CredentialResolver | None = None,
    ) -> Environment:
        """Wrap a legacy ``Config`` (or its dict form) without any ambient read.

        The config is copied and its workspace pointed at ``work_dir``;
        ``${VAR}`` placeholders are left as-is (expand them before calling).
        API keys already in the config are used as-is; ``credentials`` serves
        refs the config lacks. ``models`` lists the config's presets plus
        ``"default"`` (the implicit preset built from the flat ``agents.defaults``
        fields).
        """
        from nanobot.config.schema import Config

        if isinstance(config, Config):
            cfg = config.model_copy(deep=True)
        else:
            from nanobot.config.loader import _migrate_config

            # Deep: the migration rewrites nested sections in place.
            cfg = _validate_config(_migrate_config(copy.deepcopy(dict(config))))
        paths = _make_paths(work_dir, state_dir, strict)
        cfg.agents.defaults.workspace = str(paths.work_dir)

        if credentials is None:
            resolver: CredentialResolver = StaticCredentialResolver({})
        elif isinstance(credentials, Mapping):
            resolver = StaticCredentialResolver(credentials)
        else:
            resolver = credentials

        models = {
            name: ModelSpec(
                name=name,
                model=preset.model,
                provider=preset.provider,
                tier=preset.tier,
                context_window=preset.context_window_tokens,
                max_tokens=preset.max_tokens,
                sampling=Sampling(
                    temperature=preset.temperature, reasoning_effort=preset.reasoning_effort
                ),
                price_in=preset.price_in_per_mtok,
                price_out=preset.price_out_per_mtok,
                price_cache_read=preset.price_cache_read_per_mtok,
            )
            for name, preset in [
                ("default", cfg.resolve_default_preset()),
                *cfg.model_presets.items(),
            ]
        }
        default_model = cfg.agents.defaults.model_preset or "default"

        core = CoreEnvironment(
            config=_InMemoryConfigSource(cfg),
            credentials=resolver,
            paths=paths,
            trace=trace if trace is not None else NullTraceSink(),
            exec_base_env={},
            strict=strict,
        )
        return cls(core=core, config=cfg, models=models, default_model=default_model)

    # -- accessors -------------------------------------------------------

    @property
    def core(self) -> CoreEnvironment:
        """The kernel-internal ``CoreEnvironment``."""
        return self._core

    @property
    def config(self) -> Config:
        """The in-memory ``Config`` (internal: for kernel wiring, not host use)."""
        return self._config

    @property
    def offline(self) -> bool:
        return self._offline

    @property
    def models(self) -> Mapping[str, ModelSpec]:
        return self._models

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def paths(self) -> Paths:
        return self._core.paths

    @property
    def trace(self) -> TraceSink:
        return self._core.trace

    def __repr__(self) -> str:
        return (
            f"Environment(work_dir={self.paths.work_dir}, state_dir={self.paths.state_dir}, "
            f"models={sorted(self._models)}, default_model={self._default_model!r})"
        )


__all__ = ["Environment", "ModelSpec", "ProviderSpec"]
