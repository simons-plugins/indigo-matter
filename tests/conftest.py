"""Shared pytest fixtures for indigo-matter.

The plugin's ``Server Plugin`` directory is added to ``sys.path`` so its modules
import directly (mirroring the netro plugin's test layout, the workspace
reference). matter-server is always mocked at the WebSocket layer; the Indigo
runtime is mocked via ``mock_indigo_base``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

SERVER_PLUGIN_DIR = (
    Path(__file__).parent.parent
    / "indigo-matter.indigoPlugin"
    / "Contents"
    / "Server Plugin"
)
sys.path.insert(0, str(SERVER_PLUGIN_DIR))

#: The golden bridge-protocol frames (BRIDGE_PROTOCOL §7). THE shared location:
#: `bridge-node`'s TypeScript suite copies this same directory into its build,
#: so a frame change that only updates one side fails that side's tests.
BRIDGE_FRAMES_PATH = Path(__file__).parent / "fixtures" / "bridge_protocol" / "frames.json"


def load_bridge_frames() -> dict:
    """Parse the golden bridge-protocol frames."""
    return json.loads(BRIDGE_FRAMES_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def mock_logger():
    """A Mock logger exposing the standard logging methods."""
    logger = Mock()
    logger.debug = Mock()
    logger.info = Mock()
    logger.warning = Mock()
    logger.error = Mock()
    logger.exception = Mock()
    return logger


class _IndigoPluginBaseStub:
    """Stand-in for ``indigo.PluginBase`` at ``class Plugin`` definition time.

    Real Indigo's ``PluginBase`` does server-bound init the tests don't need;
    subclassing this stub lets ``class Plugin(indigo.PluginBase):``
    import-succeed under MagicMock test doubles.

    The device callbacks are here because the real base class **does real work**
    in them (``deviceStartComm``/``deviceStopComm`` for our own devices), so any
    override has to call ``super()`` — the SDK's most-broken rule. Each records
    the call in ``base_calls`` so a test can assert the chain-up actually
    happened rather than merely not crashing.
    """

    def _record_base_call(self, name, *args):
        calls = getattr(self, "base_calls", None)
        if calls is None:
            calls = self.base_calls = []
        calls.append((name, *args))

    def deviceCreated(self, dev):  # noqa: N802
        self._record_base_call("deviceCreated", dev)

    def deviceUpdated(self, origDev, newDev):  # noqa: N802
        self._record_base_call("deviceUpdated", origDev, newDev)

    def deviceDeleted(self, dev):  # noqa: N802
        self._record_base_call("deviceDeleted", dev)


#: Modules that `plugin` composes and that bind `indigo` at import time. They must be
#: evicted alongside `plugin` so `importlib.reload(plugin)` re-imports them against the
#: mock installed for THIS test (issue #146).
_PLUGIN_MODULES = ("plugin", "plugin_constants", "pairing_page", "http_api_mixin",
                   "export_dialog_mixin", "pairing_menu_mixin")


@pytest.fixture
def mock_indigo_base(monkeypatch):
    """Install a minimal ``indigo`` module into ``sys.modules``.

    Tests needing ``indigo.devices[id]`` lookups can extend the returned
    MagicMock with their own data and side effects.
    """
    indigo = MagicMock()
    indigo.PluginBase = _IndigoPluginBaseStub
    indigo.Dict = dict
    monkeypatch.setitem(sys.modules, "indigo", indigo)
    for name in _PLUGIN_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    return indigo
