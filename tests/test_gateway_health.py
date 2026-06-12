"""Tests for channel health reporting."""

from unittest.mock import MagicMock

from companio.channels.base import BaseChannel
from companio.bus import MessageBus


class StubChannel(BaseChannel):
    name = "stub"

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, msg):
        pass


class TestBaseChannelHealth:
    def test_health_when_stopped(self):
        bus = MagicMock(spec=MessageBus)
        ch = StubChannel(config=MagicMock(), bus=bus)
        health = ch.get_health()
        assert health == {"running": False, "status": "stopped"}

    def test_health_when_running(self):
        bus = MagicMock(spec=MessageBus)
        ch = StubChannel(config=MagicMock(), bus=bus)
        ch._running = True
        health = ch.get_health()
        assert health == {"running": True, "status": "connected"}


class TestSlackChannelHealth:
    def test_health_connected(self):
        from companio.channels.slack import SlackChannel

        config = MagicMock()
        config.bot_token = "xoxb-test"
        config.app_token = "xapp-test"
        config.allow_from = ["U123"]
        bus = MagicMock(spec=MessageBus)
        ch = SlackChannel(config, bus, workspace="/tmp")
        ch._running = True
        ch._consecutive_failures = 0
        health = ch.get_health()
        assert health["status"] == "connected"
        assert health["running"] is True

    def test_health_reconnecting(self):
        from companio.channels.slack import SlackChannel

        config = MagicMock()
        config.bot_token = "xoxb-test"
        config.app_token = "xapp-test"
        config.allow_from = ["U123"]
        bus = MagicMock(spec=MessageBus)
        ch = SlackChannel(config, bus, workspace="/tmp")
        ch._running = True
        ch._consecutive_failures = 3
        health = ch.get_health()
        assert health["status"] == "reconnecting"
        assert health["consecutive_failures"] == 3


class TestChannelManagerHealthAll:
    def test_get_health_all_with_channels(self):
        from companio.channels.manager import ChannelManager

        manager = ChannelManager.__new__(ChannelManager)
        manager.channels = {}
        bus = MagicMock(spec=MessageBus)
        stub = StubChannel(config=MagicMock(), bus=bus)
        stub._running = True
        manager.channels["stub"] = stub
        health = manager.get_health_all()
        assert "stub" in health
        assert health["stub"]["status"] == "connected"

    def test_get_health_all_empty(self):
        from companio.channels.manager import ChannelManager

        manager = ChannelManager.__new__(ChannelManager)
        manager.channels = {}
        health = manager.get_health_all()
        assert health == {}
