"""Print a JSON parity record for one checkout. See README.md. Offline, no network, no writes outside $S."""
import asyncio, inspect, json, os, sys
from pathlib import Path

S = Path(os.environ["S"])
for k in ("TELEGRAM_TOKEN", "DISCORD_TOKEN", "OPENROUTER_API_KEY", "BRAVE_API_KEY",
          "CANVAS_API_URL", "CANVAS_API_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DOCKER_HOST"):
    os.environ[k] = "placeholder"          # set BEFORE the loop is built: the exec env is snapshotted at build
ws = S / "ws"; ws.mkdir(exist_ok=True)
os.environ["MOEKA_WORKSPACE"] = str(ws)

from nanobot.config import loader
from nanobot.config.schema import Config

raw = json.load(open(S / "cfg.json"))
out = {"checkout": sys.argv[1]}
try:
    cfg = loader.resolve_config_env_vars(Config.model_validate(raw))
    out["config_valid"] = True
except Exception as exc:                   # core-slim rejects the live config
    out["config_valid"] = False
    out["config_error"] = str(exc)[:300]
    print(json.dumps(out)); sys.exit(0)

cfg.agents.defaults.workspace = str(ws)
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.cron.service import CronService
from nanobot.providers.base import LLMProvider, LLMResponse


class P(LLMProvider):
    def __init__(self):
        super().__init__(api_key="x", api_base=None, provider_name="fake")
    async def chat(self, *a, **k):
        return LLMResponse(content="hi")
    def get_default_model(self):
        return "m"


kw = {"host_tools": True} if "host_tools" in inspect.signature(AgentLoop.__init__).parameters else {}
tools = ToolRegistry()
loop = AgentLoop.from_config(cfg, MessageBus(), provider=P(), model="m",
                             cron_service=CronService(ws / "cron.json"), tool_registry=tools, **kw)
out["tools"] = sorted(tools.tool_names)
out["exec_env_keys"] = sorted(tools.get("exec")._build_env())
out["system_prompt"] = loop.context.build_system_prompt()
print(json.dumps(out))
