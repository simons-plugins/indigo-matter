"""Shared pytest fixtures for indigo-matter.

The plugin's ``Server Plugin`` directory is added to ``sys.path`` so its modules
import directly (mirroring the netro plugin's test layout, the workspace
reference). matter-server is always mocked at the WebSocket layer; the Indigo
runtime is mocked via ``mock_indigo_base``.
"""
from __future__ import annotations

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
    subclassing this empty stub lets ``class Plugin(indigo.PluginBase):``
    import-succeed under MagicMock test doubles.
    """


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
    monkeypatch.delitem(sys.modules, "plugin", raising=False)
    return indigo
