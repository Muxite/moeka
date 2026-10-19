"""Task-scoped Luna/GPU selection for daily agents."""

from __future__ import annotations

from typing import Any, cast

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import (
    ToolContext,
    current_request_context,
)
from nanobot.agent.tools.runtime_control import RuntimeControl
from nanobot.config.schema import ModelRoutingConfig

_GPU_API_BASE = "http://127.0.0.1:11437/v1"
_GPU_MODEL = "gpt-oss:20b"


class ModelSelectTool(Tool):
    """Inspect model readiness and select a preset for the current task."""

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        if ctx.runtime_control is None:
            return False
        presets = ctx.runtime_control.snapshot().model_presets
        return bool(
            (ctx.model_routing is not None and ctx.model_routing.enabled)
            or _find_preset(presets, "luna")
            or _find_preset(presets, "gpu")
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        if ctx.runtime_control is None:
            raise RuntimeError("model_select requires runtime control")
        return cls(ctx.runtime_control, ctx.model_routing)

    def __init__(
        self,
        runtime_control: RuntimeControl,
        routing: ModelRoutingConfig | None = None,
    ) -> None:
        self._runtime_control = runtime_control
        self._routing = routing or ModelRoutingConfig()

    @property
    def name(self) -> str:
        return "model_select"

    @property
    def description(self) -> str:
        return (
            "List Luna and GPU model readiness, or choose auto, luna, gpu, or a configured "
            "model preset for the next model call in this task. GPU selection makes a bounded local "
            "attempt and falls back to Luna when unavailable."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "choice": {
                    "type": "string",
                    "description": "Use 'status' to list readiness, or choose auto, luna, gpu, or a preset name.",
                },
            },
            "required": ["choice"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        choice: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        choice = choice.strip()
        snapshot = self._runtime_control.snapshot()
        presets = snapshot.model_presets
        luna_name = _find_preset(presets, "luna")
        gpu_name = _find_preset(presets, "gpu")
        if choice.casefold() in {"status", "list"}:
            gpu_ready = await _gpu_ready(
                self._routing.gpu_api_base,
                self._routing.gpu_model,
                self._routing.readiness_timeout_seconds,
            )
            luna_ready = _luna_status(self._runtime_control, luna_name)
            return ToolResult(
                "Model readiness:\n"
                f"- Luna ({luna_name or 'not configured'}): {luna_ready}\n"
                f"- GPU ({gpu_name or 'not configured'}, {self._routing.gpu_model}): "
                f"{'ready' if gpu_ready else 'unavailable'}\n"
                f"- Current: {snapshot.model_preset or 'default'} ({snapshot.model})"
            )

        normalized = choice.casefold()
        if normalized == "auto":
            target = luna_name
        elif normalized == "luna":
            target = luna_name
        elif normalized == "gpu":
            target = gpu_name
        else:
            target = next((name for name in presets if name.casefold() == normalized), None)
        if target is None:
            available = ", ".join(presets) or "none"
            return ToolResult.error(f"Model choice {choice!r} is not configured. Available: {available}")

        request_context = current_request_context()
        if (
            request_context is not None
            and request_context.attributes.get("_user_model_lock") is True
        ):
            return ToolResult(
                "The user explicitly selected a model for this task; keep using that selection."
            )
        try:
            runtime = self._runtime_control.resolve_model_preset(target)
        except (KeyError, ValueError) as exc:
            return ToolResult.error(str(exc))
        if request_context is not None:
            request_context.attributes["_model_select_runtime"] = runtime
            request_context.attributes["_requested_model_preset"] = normalized
        readiness = ""
        if normalized == "gpu":
            gpu_ready = await _gpu_ready(
                self._routing.gpu_api_base,
                self._routing.gpu_model,
                self._routing.readiness_timeout_seconds,
            )
            readiness = (
                " The local GPU endpoint is ready."
                if gpu_ready
                else " The local GPU endpoint is unavailable now; a bounded attempt will fall back to Luna."
            )
        return ToolResult(
            f"Selected {target!r} ({runtime.model}) for the next model call in this task. "
            f"The next task starts with the configured automatic model.{readiness}"
        )


def _find_preset(presets: dict[str, dict[str, object]], alias: str) -> str | None:
    for name, values in presets.items():
        model = str(values.get("model", "")).casefold()
        provider = str(values.get("provider", "")).casefold()
        if alias == "luna" and ("gpt-6-luna" in model or name.casefold() == "luna"):
            return name
        if alias == "gpu" and (
            provider == "moeka_gpu" or _GPU_MODEL in model or name.casefold() == "gpu"
        ):
            return name
    return None


def _luna_status(runtime_control: RuntimeControl, preset_name: str | None) -> str:
    if preset_name is None:
        return "not configured"
    try:
        runtime = runtime_control.resolve_model_preset(preset_name)
    except (KeyError, ValueError):
        return "not configured"
    return "missing credentials" if runtime.provider.provider_name == "unconfigured" else "ready"


async def _gpu_ready(api_base: str, model: str, timeout_s: float) -> bool:
    """Probe the dedicated local Ollama endpoint with a strict two-second bound."""
    try:
        import httpx

        readiness_url = api_base.rstrip("/").removesuffix("/v1") + "/api/tags"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(readiness_url)
        if response.status_code != 200:
            return False
        payload: object = response.json()
        if not isinstance(payload, dict):
            return False
        typed_payload = cast(dict[str, object], payload)
        models = typed_payload.get("models")
        if not isinstance(models, list):
            return False
        expected = {model, f"{model}:latest"}
        return any(
            _is_gpu_model(item, expected) for item in cast(list[object], models)
        )
    except Exception:  # endpoint is an optional local capability
        return False


def _is_gpu_model(item: object, expected: set[str]) -> bool:
    if not isinstance(item, dict):
        return False
    return cast(dict[str, object], item).get("name") in expected
