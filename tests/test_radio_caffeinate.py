"""Tests that the radio-garden dispatch wraps _run_radio with caffeinate."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import detect.cli as cli_mod


@contextmanager
def _noop_caffeinate():
    yield


def _radio_args(**kwargs):
    defaults = dict(
        detect_command="radio-garden",
        url="http://radio.example.com",
        interval=30,
        capture=10,
        duration=0,
        cooldown=5,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_radio_garden_wrapped_with_caffeinate():
    entered = []

    @contextmanager
    def _tracking():
        entered.append("enter")
        yield
        entered.append("exit")

    with (
        patch("detect.cli.caffeinate", _tracking),
        patch("detect.cli.migrate"),
        patch("detect.cli.asyncio.run"),
    ):
        cli_mod.dispatch(_radio_args(), MagicMock())

    assert entered == ["enter", "exit"]


def test_radio_garden_asyncio_run_called_inside_caffeinate():
    """asyncio.run must be called while caffeinate is active (not before/after)."""
    order = []

    @contextmanager
    def _tracking():
        order.append("caffeinate_start")
        yield
        order.append("caffeinate_end")

    def _fake_asyncio_run(coro):
        coro.close()
        order.append("asyncio_run")

    with (
        patch("detect.cli.caffeinate", _tracking),
        patch("detect.cli.migrate"),
        patch("detect.cli.asyncio.run", _fake_asyncio_run),
    ):
        cli_mod.dispatch(_radio_args(), MagicMock())

    assert order == ["caffeinate_start", "asyncio_run", "caffeinate_end"]


def test_capture_gte_interval_exits_before_caffeinate():
    """Validation error (capture >= interval) should abort before caffeinate starts."""
    entered = []

    @contextmanager
    def _tracking():
        entered.append("enter")
        yield

    with (
        patch("detect.cli.caffeinate", _tracking),
        patch("detect.cli.migrate"),
        patch("detect.cli.sys.exit", side_effect=SystemExit),
    ):
        try:
            cli_mod.dispatch(_radio_args(capture=30, interval=30), MagicMock())
        except SystemExit:
            pass

    assert entered == [], "caffeinate must not start when the args are invalid"
