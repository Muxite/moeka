"""AgentLoop no longer depends on the channels/gateway config sections."""
import inspect
from unittest.mock import MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus


def test_agent_loop_has_no_channels_config(tmp_path):
    assert "channels_config" not in inspect.signature(AgentLoop.__init__).parameters
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    assert not hasattr(loop, "channels_config")


def test_restart_mode_default_is_auto(tmp_path):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    assert loop.restart_mode == "auto"
