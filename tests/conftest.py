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


class _IndigoDict(dict):
    """Stand-in for ``indigo.Dict`` that is a DISTINCT type from ``dict``.

    This was plain ``dict`` until issue #186, which made an entire bug class
    invisible. Indigo's ConfigUI pre-population callbacks must return
    ``indigo.Dict``; returning a plain dict fails inside Indigo's C++ bridge
    ("No registered converter was able to extract a C++ reference to type
    CXmlDict from this Python object of type dict") and silently seeds nothing.
    That shipped, cost an hour on jarvis — and the regression test written
    afterwards could not fail, because ``isinstance(x, indigo.Dict)`` was
    ``isinstance(x, dict)``. Proven by reverting the fix: the whole suite
    stayed green.

    Subclassing dict keeps every existing test working (the mapping behaves
    identically) while making the isinstance check mean something.
    """


class _IndigoList(list):
    """Same reasoning as :class:`_IndigoDict`, for ``indigo.List``."""


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

    #: ``{deviceTypeId: [state dicts]}`` standing in for Devices.xml's <States>.
    #: Tests populate it; the same list object is handed back on every call
    #: BECAUSE that is what real Indigo does — the return value is the XML
    #: parser's internal cache for the type, not a fresh copy, so an override
    #: that mutates it in place corrupts every later call and eventually the XML
    #: serialiser. Sharing the object here is what lets a test catch that, and
    #: it must be shared across DEVICES of a type, not merely across calls for
    #: one device — two relays on one server is the real corruption case.
    #:
    #: Built per instance in ``__init__`` rather than left as a class attribute:
    #: a mutable class-level dict would leak a test's entries into every later
    #: test in the session, which is an order-dependent failure and miserable to
    #: diagnose.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_state_lists: dict = {}

    def getDeviceStateList(self, dev):  # noqa: N802
        self._record_base_call("getDeviceStateList", dev)
        # Real Indigo always has a <States> block for a type it knows about, so
        # an empty list — never None — is the honest stand-in for "no states".
        return self.base_state_lists.get(dev.deviceTypeId, [])


#: Every module `plugin` composes. The mixins bind `indigo` at import time and MUST
#: be evicted alongside `plugin` so `importlib.reload(plugin)` re-imports them against
#: the mock installed for THIS test; `plugin_constants`/`pairing_page` bind no indigo
#: but are listed so the rule stays simply "everything plugin composes" (issue #146).
#: Eviction uses raising=False, so a typo'd entry would no-op silently —
#: test_plugin_module.py pins each entry to a real Server Plugin file.
_PLUGIN_MODULES = ("plugin", "plugin_constants", "pairing_page", "http_api_mixin",
                   "export_dialog_mixin", "pairing_menu_mixin", "matter_server_menu_mixin",
                   "export_recovery_menu_mixin", "bridge_agent_menu_mixin",
                   "diagnostics_menu_mixin")


@pytest.fixture
def mock_indigo_base(monkeypatch):
    """Install a minimal ``indigo`` module into ``sys.modules``.

    Tests needing ``indigo.devices[id]`` lookups can extend the returned
    MagicMock with their own data and side effects.
    """
    indigo = MagicMock()
    indigo.PluginBase = _IndigoPluginBaseStub
    indigo.Dict = _IndigoDict
    indigo.List = _IndigoList
    monkeypatch.setitem(sys.modules, "indigo", indigo)
    for name in _PLUGIN_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    return indigo
