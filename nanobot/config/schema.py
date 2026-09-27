"""Configuration schema using Pydantic."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from pydantic import AliasChoices, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from nanobot.config.timezone import detect_system_timezone
from nanobot.config_base import Base

if TYPE_CHECKING:
    from nanobot.agent.runner import RunnerLimits
    from nanobot.agent.tools.filesystem import FileToolsConfig
    from nanobot.agent.tools.image_generation import ImageGenerationToolConfig
    from nanobot.agent.tools.self import MyToolConfig
    from nanobot.agent.tools.shell import ExecToolConfig
    from nanobot.agent.tools.web import WebToolsConfig


class DisplayConfig(Base):
    """How the interactive CLI surfaces progress while the agent works."""

    send_progress: bool = True  # print the agent's text progress
    send_tool_hints: bool = True  # print tool-call hints (e.g. read_file("…"))
    show_reasoning: bool = True  # print model reasoning when the provider streams it


class DreamConfig(Base):
    """Dream memory consolidation configuration.

    Core has no scheduler: Dream runs when a caller invokes it directly
    (``AgentLoop.run_dream()`` or the ``/dream`` command).
    """

    enabled: bool = True  # Whether callers/harnesses should run Dream at all
    # Advisory cadence in hours; used by the caller/harness that invokes Dream,
    # core itself never schedules it.
    interval_h: int = Field(default=2, ge=1)
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Model preset name for Dream sessions

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_cron(cls, data: Any) -> Any:
        """Accept old configs carrying the retired ``cron`` override, dropping it."""
        if isinstance(data, dict) and "cron" in data:
            data = dict(cast(dict[str, Any], data))
            value = data.pop("cron")
            if value is not None:
                from loguru import logger

                logger.warning(
                    "Ignoring retired config key agents.defaults.dream.cron={!r}: "
                    "Dream is no longer scheduled by nanobot; the caller invokes it "
                    "(see dream.intervalH).",
                    value,
                )
        return data


class VecConfig(Base):
    """Semantic vector memory configuration."""

    enable: bool = True
    embedding_model: str = "all-MiniLM-L6-v2"
    # Chars threshold above which MEMORY.md is retrieved semantically instead of injected in full.
    memory_semantic_threshold: int = 2048
    # Always include this many most-recent history entries (recency anchor).
    history_recent_k: int = 15
    # Pull this many additional semantically-relevant entries from older history.
    history_semantic_k: int = 10
    # Maximum skills to surface by semantic relevance when the skills list is long.
    skills_top_k: int = 10
    # How many memory chunks to pull per semantic query.
    memory_top_k: int = 10
    # Record every retrieval (query, store, returned chunks) to a retrieval_log
    # table in vec.db for observability. Off by default.
    log_retrievals: bool = False


class InlineFallbackConfig(Base):
    """One inline fallback model configuration."""

    model: str
    provider: str
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


FallbackCandidate = str | InlineFallbackConfig


class ModelPresetConfig(Base):
    """A named set of model + generation parameters for quick switching."""

    model: str
    provider: str = "auto"
    max_tokens: int = 8192
    context_window_tokens: int = 200_000
    temperature: float = 0.1
    reasoning_effort: str | None = None
    # Cost ledger (kernel ledger, I6). All optional; absent = unknown, never free.
    # ``tier="local"`` with no prices is the one convention for cost 0 (self-hosted).
    # Prices are USD per million tokens; cache writes bill at the input price.
    tier: Literal["local", "fast", "standard", "frontier"] | None = None
    price_in_per_mtok: float | None = Field(default=None, ge=0)
    price_out_per_mtok: float | None = Field(default=None, ge=0)
    price_cache_read_per_mtok: float | None = Field(default=None, ge=0)

    def to_generation_settings(self) -> Any:
        from nanobot.providers.base import GenerationSettings
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.nanobot"
    model_preset: str | None = None  # Active preset name — takes precedence over fields below
    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 200_000
    temperature: float = 0.1
    fallback_models: list[FallbackCandidate] = Field(default_factory=list)
    max_tool_iterations: int = 200
    max_concurrent_subagents: int = Field(default=4, ge=1)
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    tool_hint_max_length: int = Field(
        default=40,
        ge=20,
        le=500,
        validation_alias=AliasChoices("toolHintMaxLength"),
        serialization_alias="toolHintMaxLength",
    )  # Max characters for tool hint display (e.g. "$ cd …/project && npm test")
    reasoning_effort: str | None = None  # low / medium / high / xhigh / max / adaptive / none — LLM thinking effort; None preserves the provider default
    timezone: str = "UTC"  # Effective IANA timezone, e.g. "Asia/Shanghai"
    timezone_mode: Literal["auto", "manual"] = "auto"
    bot_name: str = "nanobot"  # Display name shown in CLI prompts (e.g. "{name} is thinking...")
    bot_icon: str = "🐈"  # Short icon (emoji or text) shown next to the bot name in CLI; "" to omit
    unified_session: bool = False  # Share one session across all channels (single-user multi-device)
    disabled_skills: list[str] = Field(default_factory=list)  # Skill names to exclude from loading (e.g. ["summarize", "skill-creator"])
    session_ttl_minutes: int = Field(
        default=15,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )  # Auto-compact idle threshold in minutes (0 = disabled)
    idle_compact_check_interval_seconds: int = Field(
        default=60,
        ge=0,
    )  # Minimum interval in seconds between scans for idle sessions
    dream: DreamConfig = Field(default_factory=DreamConfig)
    vec: VecConfig = Field(default_factory=lambda: VecConfig())
    tools_allow: list[str] | None = None  # None = all tools; [] = no tools
    tools_deny: list[str] = Field(default_factory=list)  # tool names never registered
    allowed_skills: list[str] | None = None  # None = all skills (minus disabled_skills)
    planning: bool = False  # plan-then-execute: one pre-run LLM call injects a plan note
    limits: RunnerLimits = Field(
        default_factory=lambda: _lazy_default("nanobot.agent.runner", "RunnerLimits"),
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_timezone(cls, value: object) -> object:
        """Detect new defaults server-side while preserving configured timezones."""
        if not isinstance(value, dict):
            return value

        data = dict(cast(dict[str, object], value))
        timezone_mode = data.get("timezoneMode", data.get("timezone_mode"))
        if timezone_mode is None:
            timezone_mode = "manual" if "timezone" in data else "auto"
            data["timezoneMode"] = timezone_mode
        if timezone_mode == "auto":
            data["timezone"] = detect_system_timezone()
        return data

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown timezone {value!r}") from None
        return value


class InlineSkillConfig(Base):
    """A skill defined in code rather than as a SKILL.md on disk.

    ``metadata`` mirrors the SKILL.md nanobot frontmatter payload
    (``always: bool``, ``requires: {bins: [...], env: [...]}``). Inline skills
    shadow workspace/builtin skills of the same name and are exempt from
    ``skills_include`` filtering (the host registered them explicitly), but
    still honor ``skills_exclude``.
    """

    name: str
    content: str
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentProfileConfig(Base):
    """A named scoping bundle: model preset, persona, tools, skills, limits.

    Resolved by embedding hosts via ``MoekaCore.create(profile=...)``; fields
    left at their defaults inherit the corresponding ``agents.defaults``
    behavior. ``tools_allow`` is a hard allowlist applied at tool-discovery
    time, so tools added to moeka later never silently appear in a scoped agent.
    """

    model_preset: str | None = None
    system_prompt: str | None = None  # inline persona text (wins over system_prompt_file)
    system_prompt_file: str | None = None  # persona file, read once at create time
    tools_allow: list[str] | None = None  # None = all tools; [] = no tools
    tools_deny: list[str] = Field(default_factory=list)
    skills_include: list[str] | None = None  # None = all skills (minus exclude); [] = none
    skills_exclude: list[str] = Field(default_factory=list)
    skills_inline: list[InlineSkillConfig] = Field(default_factory=list)  # in-code skills
    memory_enabled: bool = True  # False disables semantic vec memory for this profile
    vec_collections: list[str] = Field(default_factory=list)  # document collections this profile uses
    planning: bool = False  # plan-then-execute for this profile
    limits: RunnerLimits | None = None


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    # User-facing name for dynamic custom providers.
    display_name: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    api_type: Literal["auto", "chat_completions", "responses"] = "auto"  # Request API surface
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    extra_body: dict[str, Any] | None = None  # Extra provider request fields; shape depends on provider/API surface
    extra_query: dict[str, str] | None = None  # Extra query params (e.g. api-version for Azure-style gateways)
    proxy: str | None = None  # Explicit HTTP proxy; image downloads trust its DNS and egress
    thinking_style: str | None = None  # Thinking/reasoning style for custom providers

    # Valid values mirror the keys of _THINKING_STYLE_MAP in
    # nanobot/providers/openai_compat_provider.py. Kept duplicated here to
    # avoid an import cycle (schema.py must not import from providers/).
    _VALID_THINKING_STYLES: ClassVar[tuple[str, ...]] = (
        "thinking_type",
        "enable_thinking",
        "reasoning_split",
    )

    @field_validator("thinking_style")
    @classmethod
    def _validate_thinking_style(cls, v: str | None) -> str | None:
        if not v:  # None or "" -> no injection, valid (backwards compatible)
            return v
        if v not in cls._VALID_THINKING_STYLES:
            raise ValueError(
                f"Invalid thinking_style {v!r}. "
                f"Must be one of: {', '.join(repr(s) for s in cls._VALID_THINKING_STYLES)} "
                f"(or empty/omitted)."
            )
        return v


class BedrockProviderConfig(ProviderConfig):
    """AWS Bedrock Runtime provider configuration."""

    region: str | None = None  # AWS region, falls back to AWS_REGION/AWS_DEFAULT_REGION/profile
    profile: str | None = None  # Optional AWS shared config profile


class ProvidersConfig(Base):
    """Configuration for LLM providers.

    Supports custom providers via extra fields — any additional field
    becomes an OpenAI-compatible custom provider.
    """

    model_config = ConfigDict(extra="allow")

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    bedrock: BedrockProviderConfig = Field(default_factory=BedrockProviderConfig)  # AWS Bedrock Converse
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    orcarouter: ProviderConfig = Field(default_factory=ProviderConfig)  # OrcaRouter API gateway
    assemblyai: ProviderConfig = Field(default_factory=ProviderConfig)  # AssemblyAI voice transcription
    huggingface: ProviderConfig = Field(default_factory=ProviderConfig)
    skywork: ProviderConfig = Field(default_factory=ProviderConfig)  # Skywork / APIFree API gateway
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    modelscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig)  # LM Studio local models
    atomic_chat: ProviderConfig = Field(default_factory=ProviderConfig)  # Atomic Chat local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    kimi_coding: ProviderConfig = Field(default_factory=ProviderConfig)  # Kimi Coding Plan (Anthropic Messages API)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig)  # MiniMax Anthropic endpoint (thinking)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰) — LLM + ASR (set apiBase to Plan URL for ASR)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    longcat: ProviderConfig = Field(default_factory=ProviderConfig)  # LongCat
    ant_ling: ProviderConfig = Field(default_factory=ProviderConfig)  # Ant Ling
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    edenai: ProviderConfig = Field(default_factory=ProviderConfig)  # Eden AI API gateway
    novita: ProviderConfig = Field(default_factory=ProviderConfig)  # Novita AI
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    xai_grok: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # xAI Grok (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)
    nvidia: ProviderConfig = Field(default_factory=ProviderConfig)  # NVIDIA NIM (nvapi- keys)
    opencode: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenCode Zen (canonical provider id)
    opencode_zen: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenCode Zen (curated coding models)
    opencode_go: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenCode Go (low-cost coding models)

    @model_validator(mode="after")
    def convert_extra_providers(self):
        """Convert extra fields (custom providers) to ProviderConfig objects."""
        if self.model_extra:
            from nanobot.providers.registry import find_by_name

            for key, value in self.model_extra.items():
                if spec := find_by_name(key):
                    raise ValueError(
                        f"providers.{key} conflicts with built-in provider {spec.name!r}; "
                        "use the built-in provider key or choose a different custom provider name"
                    )
                if isinstance(value, dict):
                    self.model_extra[key] = ProviderConfig.model_validate(value)
        return self

    @model_validator(mode="after")
    def _validate_api_type_scope(self) -> "ProvidersConfig":
        for name in self.__class__.model_fields:
            if name == "openai":
                continue
            provider = getattr(self, name, None)
            if isinstance(provider, ProviderConfig) and provider.api_type != "auto":
                raise ValueError("providers.<name>.api_type is only supported for providers.openai")
        for provider in (self.model_extra or {}).values():
            if isinstance(provider, ProviderConfig) and provider.api_type != "auto":
                raise ValueError("providers.<name>.api_type is only supported for providers.openai")
        return self


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    auth: Literal["oauth"] | None = None  # Remote MCP OAuth; tokens are stored outside config
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    cwd: str = ""  # Stdio: working directory for MCP server runtime artifacts
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all capabilities (tools, resources, prompts); any restriction = only listed tools, no resources/prompts


def _lazy_default(module_path: str, class_name: str) -> Any:
    """Deferred import helper for ToolsConfig default factories."""
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


class ToolsConfig(Base):
    """Tools configuration.

    Field types for tool-specific sub-configs are resolved via model_rebuild()
    at the bottom of this file so tool config classes can stay next to their
    tool implementations.
    """

    web: WebToolsConfig = Field(default_factory=lambda: _lazy_default("nanobot.agent.tools.web", "WebToolsConfig"))
    exec: ExecToolConfig = Field(default_factory=lambda: _lazy_default("nanobot.agent.tools.shell", "ExecToolConfig"))
    file: FileToolsConfig = Field(default_factory=lambda: _lazy_default("nanobot.agent.tools.filesystem", "FileToolsConfig"))
    my: MyToolConfig = Field(default_factory=lambda: _lazy_default("nanobot.agent.tools.self", "MyToolConfig"))
    image_generation: ImageGenerationToolConfig = Field(
        default_factory=lambda: _lazy_default("nanobot.agent.tools.image_generation", "ImageGenerationToolConfig"),
    )
    max_session_messages_per_minute: int = Field(default=6, ge=1)
    restrict_to_workspace: bool = False  # policy intent: keep tool access inside workspace when possible
    webui_allow_local_service_access: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "webuiAllowLocalServiceAccess",
            "webui_allow_local_service_access",
            "allowLocalPreviewAccess",
            "allow_local_preview_access",
        ),
    )  # allow WebUI Full Access shell checks against localhost services; legacy allowLocalPreviewAccess still reads
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class Config(BaseSettings):
    """Root configuration for nanobot."""

    _source_path: Path | None = PrivateAttr(default=None)

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
        serialization_alias="modelPresets",
    )
    profiles: dict[str, AgentProfileConfig] = Field(default_factory=dict)

    def __init__(self, **values: Any) -> None:
        if not type(self).__pydantic_complete__:
            _resolve_tool_config_refs()
        super().__init__(**values)

    def bind_source_path(self, path: Path) -> None:
        """Record the config file that owns instance-level runtime data."""
        self._source_path = path.expanduser().resolve(strict=False)

    @property
    def source_path(self) -> Path | None:
        """The ``config.json`` this config was loaded from (``None`` = in-memory)."""
        return self._source_path

    @property
    def runtime_data_dir(self) -> Path | None:
        """Return the active instance data directory when loaded from a config path."""
        return self._source_path.parent if self._source_path is not None else None

    @model_validator(mode="after")
    def _validate_model_preset(self) -> "Config":
        # Keep persisted names accepted by previous releases loadable. New
        # names are normalized and checked case-insensitively at mutation
        # boundaries, where conflicts can be reported without breaking startup.
        if "default" in self.model_presets:
            raise ValueError("model_preset name 'default' is reserved for agents.defaults")
        name = self.agents.defaults.model_preset
        if name and name != "default" and name not in self.model_presets:
            raise ValueError(f"model_preset {name!r} not found in model_presets")
        dream_name = self.agents.defaults.dream.model_override
        if dream_name and dream_name != "default" and dream_name not in self.model_presets:
            raise ValueError(f"Dream model preset {dream_name!r} not found in model_presets")
        for fallback in self.agents.defaults.fallback_models:
            if isinstance(fallback, str) and fallback not in self.model_presets:
                raise ValueError(f"fallback_models entry {fallback!r} not found in model_presets")
        for profile_name, profile in self.profiles.items():
            preset = profile.model_preset
            if preset and preset != "default" and preset not in self.model_presets:
                raise ValueError(
                    f"profile {profile_name!r} references unknown model_preset {preset!r}"
                )
        return self

    def resolve_profile(self, name: str) -> AgentProfileConfig:
        """Return the named agent profile, raising ``KeyError`` when missing."""
        if name not in self.profiles:
            raise KeyError(f"profile {name!r} not found in profiles")
        return self.profiles[name]

    def resolve_default_preset(self) -> ModelPresetConfig:
        """Return the implicit `default` preset from agents.defaults fields."""
        d = self.agents.defaults
        return ModelPresetConfig(
            model=d.model, provider=d.provider, max_tokens=d.max_tokens,
            context_window_tokens=d.context_window_tokens,
            temperature=d.temperature, reasoning_effort=d.reasoning_effort,
        )

    def resolve_preset(self, name: str | None = None) -> ModelPresetConfig:
        """Return effective model params from a named preset or the implicit default."""
        name = self.agents.defaults.model_preset if name is None else name
        if not name or name == "default":
            return self.resolve_default_preset()
        if name not in self.model_presets:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return self.model_presets[name]

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path.

        Falls back to the default state home when the workspace string still
        contains an unexpanded ``${VAR}`` placeholder (e.g. ``MOEKA_WORKSPACE``
        was not set in the environment).  This prevents a literal directory
        named ``${MOEKA_WORKSPACE}`` from being created inside the repo.
        """
        ws = self.agents.defaults.workspace
        if "${" in ws:
            from nanobot.config.loader import get_state_home
            return get_state_home()
        return Path(ws).expanduser()

    def match_provider(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
        has_credential: Callable[[str], bool] | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name).

        A provider slot counts as keyed when its ``api_key`` is set in config or,
        with *has_credential*, when the host credential resolver has a key for
        that slot name (kernel hosts that keep keys out of config). ``None``
        keeps the config-only rule.
        """
        return self._match_provider(model, preset=preset, has_credential=has_credential)

    def _match_provider(
        self, model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
        has_credential: Callable[[str], bool] | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from nanobot.providers.registry import (
            PROVIDERS,
            find_by_name,
        )

        resolved = preset or self.resolve_preset()
        forced = resolved.provider

        def _keyed(p: ProviderConfig, name: str) -> bool:
            return bool(p.api_key) or (has_credential is not None and has_credential(name))

        def _custom_provider_by_name(name: str) -> tuple[ProviderConfig, str] | None:
            normalized = name.replace("-", "_").lower()
            for attr_name, provider in (self.providers.model_extra or {}).items():
                if not isinstance(provider, ProviderConfig):
                    continue
                if attr_name.replace("-", "_").lower() == normalized:
                    return provider, attr_name
            return None

        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            custom = _custom_provider_by_name(forced)
            if custom is not None:
                return custom
            return None, None

        model_lower = (model or resolved.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")
        prefixed_provider = find_by_name(model_prefix) if model_prefix else None

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            if spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or spec.is_direct or _keyed(p, spec.name):
                    return p, spec.name

        # Check for custom provider by prefix (e.g., "companyProxy/gpt-4").
        # Return the matching provider even when apiBase is missing, so a
        # malformed explicit prefix fails instead of falling through to a
        # different custom provider.
        if model_prefix:
            custom = _custom_provider_by_name(normalized_prefix)
            if custom is not None:
                return custom

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            if spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                # Local providers (Ollama, vLLM, …) keep model-family keywords
                # like "nemotron" or "llama" to enable bare-model auto-routing,
                # but those keywords collide with cloud-hosted variants of the
                # same family (e.g. `nvidia/nemotron-...` via OpenRouter). Only
                # honor a local keyword match when the user has actually
                # configured that local endpoint via `api_base` — mirrors the
                # gate already used by the local-fallback loop below.
                if spec.is_local:
                    # A qualified model belongs to its explicit provider or a
                    # gateway fallback, never to a different local provider
                    # whose model-family keyword happens to match.
                    foreign_prefix = bool(
                        prefixed_provider is not None and prefixed_provider.name != spec.name
                    )
                    if not p.api_base or foreign_prefix:
                        continue
                if spec.is_oauth or spec.is_local or spec.is_direct or _keyed(p, spec.name):
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        if prefixed_provider is None:
            for spec in PROVIDERS:
                if not spec.is_local:
                    continue
                p = getattr(self.providers, spec.name, None)
                if not (p and p.api_base):
                    continue
                if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                    return p, spec.name
                if local_fallback is None:
                    local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth or spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and _keyed(p, spec.name):
                return p, spec.name

        # Final fallback: check for any configured custom provider
        for attr_name, p in (self.providers.model_extra or {}).items():
            if isinstance(p, ProviderConfig) and p.api_base:
                return p, attr_name

        return None, None

    def get_provider(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
        has_credential: Callable[[str], bool] | None = None,
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model, preset=preset, has_credential=has_credential)
        return p

    def get_provider_name(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
        has_credential: Callable[[str], bool] | None = None,
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model, preset=preset, has_credential=has_credential)
        return name

    def get_api_key(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
        has_credential: Callable[[str], bool] | None = None,
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model, preset=preset, has_credential=has_credential)
        return p.api_key if p else None

    def get_api_base(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
        has_credential: Callable[[str], bool] | None = None,
    ) -> str | None:
        """Get API base URL for the given model, falling back to the provider default when present."""
        from nanobot.providers.registry import find_by_name

        p, name = self._match_provider(model, preset=preset, has_credential=has_credential)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = SettingsConfigDict(
        env_prefix="NANOBOT_",
        env_nested_delimiter="__",
    )


def _resolve_tool_config_refs() -> None:
    """Resolve forward references in ToolsConfig by importing tool config classes.

    Must be called after all modules are loaded (breaks circular imports).
    Re-exports the classes into this module's namespace so existing imports
    like ``from nanobot.config.schema import ExecToolConfig`` continue to work.
    """
    import sys

    from nanobot.agent.runner import RunnerLimits
    from nanobot.agent.tools.filesystem import FileToolsConfig
    from nanobot.agent.tools.image_generation import ImageGenerationToolConfig
    from nanobot.agent.tools.self import MyToolConfig
    from nanobot.agent.tools.shell import ExecToolConfig
    from nanobot.agent.tools.web import WebFetchConfig, WebSearchConfig, WebToolsConfig

    # Re-export into this module's namespace
    mod = sys.modules[__name__]
    mod.ExecToolConfig = ExecToolConfig  # type: ignore[attr-defined]
    mod.FileToolsConfig = FileToolsConfig  # type: ignore[attr-defined]
    mod.WebToolsConfig = WebToolsConfig  # type: ignore[attr-defined]
    mod.WebSearchConfig = WebSearchConfig  # type: ignore[attr-defined]
    mod.WebFetchConfig = WebFetchConfig  # type: ignore[attr-defined]
    mod.MyToolConfig = MyToolConfig  # type: ignore[attr-defined]
    mod.ImageGenerationToolConfig = ImageGenerationToolConfig  # type: ignore[attr-defined]
    mod.RunnerLimits = RunnerLimits  # type: ignore[attr-defined]

    AgentDefaults.model_rebuild()
    AgentProfileConfig.model_rebuild()
    ToolsConfig.model_rebuild()
    Config.model_rebuild()


# Eagerly resolve when the import chain allows it (no circular deps at this
# point).  If it fails (first import triggers a cycle), the rebuild will
# happen lazily when Config/ToolsConfig is first used at runtime.
_TOOL_REFS_RESOLVED = False


def _ensure_tool_config_refs_resolved() -> None:
    """Lazy retry hook for the rebuild — call before instantiating Config."""
    global _TOOL_REFS_RESOLVED
    if _TOOL_REFS_RESOLVED:
        return
    try:
        _resolve_tool_config_refs()
        _TOOL_REFS_RESOLVED = True
    except ImportError:
        pass


try:
    _resolve_tool_config_refs()
    _TOOL_REFS_RESOLVED = True
except ImportError:
    pass


_orig_config_init = Config.__init__
_orig_tools_init = ToolsConfig.__init__
_orig_agent_defaults_init = AgentDefaults.__init__
_orig_agent_profile_init = AgentProfileConfig.__init__


def _config_init_with_lazy_rebuild(self, *args: Any, **kwargs: Any) -> None:
    _ensure_tool_config_refs_resolved()
    _orig_config_init(self, *args, **kwargs)


def _tools_init_with_lazy_rebuild(self, *args: Any, **kwargs: Any) -> None:
    _ensure_tool_config_refs_resolved()
    _orig_tools_init(self, *args, **kwargs)


def _agent_defaults_init_with_lazy_rebuild(self, *args: Any, **kwargs: Any) -> None:
    _ensure_tool_config_refs_resolved()
    _orig_agent_defaults_init(self, *args, **kwargs)


def _agent_profile_init_with_lazy_rebuild(self, *args: Any, **kwargs: Any) -> None:
    _ensure_tool_config_refs_resolved()
    _orig_agent_profile_init(self, *args, **kwargs)


Config.__init__ = _config_init_with_lazy_rebuild  # type: ignore[method-assign]
ToolsConfig.__init__ = _tools_init_with_lazy_rebuild  # type: ignore[method-assign]
AgentDefaults.__init__ = _agent_defaults_init_with_lazy_rebuild  # type: ignore[method-assign]
AgentProfileConfig.__init__ = _agent_profile_init_with_lazy_rebuild  # type: ignore[method-assign]
