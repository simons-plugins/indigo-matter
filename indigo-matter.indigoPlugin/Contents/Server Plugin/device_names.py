"""How a log line names an Indigo device - shared by ``plugin`` and ``device_sync``."""
from __future__ import annotations


def device_log_name(dev_id) -> str:
    """``"Name"`` (the plugin's own log style), or ``device <id>`` if the lookup fails -
    a line must still log."""
    import indigo  # call-time: the Indigo runtime (or the test mock) in force right now
    try:
        return f'"{indigo.devices[int(dev_id)].name}"'
    except Exception:  # pylint: disable=broad-except
        # gone, or Indigo failed to answer: the id is all there is, and enough
        return f"device {dev_id}"
