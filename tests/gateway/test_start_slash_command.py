"""Regression test: /start slash command toggles free_response_channels."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin


class _FakeRunner(GatewaySlashCommandsMixin):
    """Minimal runner: only the pieces _handle_start_command touches."""

    def __init__(self, config_path, platform_key="discord"):
        self._config_path = config_path
        self._platform_key = platform_key
        self.written = {}

    def _display_config_target(self, event):
        return self._config_path, self._platform_key


def _make_event(chat_id):
    source = SimpleNamespace(platform="discord", chat_id=str(chat_id))
    return SimpleNamespace(source=source, get_command_args=lambda: "")


@pytest.mark.asyncio
async def test_start_toggles_free_response_channel_on(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("platforms:\n  discord:\n    free_response_channels: \"\"\n")
    runner = _FakeRunner(cfg)

    with patch("gateway.run._load_gateway_config") as load, \
         patch("gateway.slash_commands._write_raw_config_leaf") as write:
        load.return_value = {"platforms": {"discord": {"free_response_channels": ""}}}
        write.side_effect = lambda p, keys, val: runner.written.update({tuple(keys): val})

        result = await runner._handle_start_command(_make_event(555111222))

    assert "ENABLED" in result
    assert runner.written[("platforms", "discord", "free_response_channels")] == "555111222"


@pytest.mark.asyncio
async def test_start_toggles_free_response_channel_off(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("platforms:\n  discord:\n    free_response_channels: \"\"\n")
    runner = _FakeRunner(cfg)

    with patch("gateway.run._load_gateway_config") as load, \
         patch("gateway.slash_commands._write_raw_config_leaf") as write:
        load.return_value = {"platforms": {"discord": {"free_response_channels": "555111222,999888777"}}}
        write.side_effect = lambda p, keys, val: runner.written.update({tuple(keys): val})

        result = await runner._handle_start_command(_make_event(555111222))

    assert "DISABLED" in result
    assert runner.written[("platforms", "discord", "free_response_channels")] == "999888777"
