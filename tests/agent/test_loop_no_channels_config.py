"""AgentLoop takes the channels/gateway config only as optional host seams."""
from unittest.mock import MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus


def test_bare_agent_loop_has_no_host_channel_wiring(tmp_path):
    """A kernel-style loop carries no channel config, no host tools and no automation turns."""
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    assert loop.channels_config is None
    assert loop.host_tools is False
    assert loop._automation_turn_coordinators == ()
    assert loop.pending_cron_job_ids_for_session("x") == set()
    assert "message" not in loop.tools.tool_names


def test_restart_mode_default_is_auto(tmp_path):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    assert loop.restart_mode == "auto"
