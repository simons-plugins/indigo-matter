"""Stateful fake Indigo devices/device-command-namespace for INBOUND tests.

Used by test_device_sync.py, test_generic_switch.py, test_power_source.py,
and test_integration.py to stand in for ``indigo.devices``/``indigo.device``
when exercising device_sync's reconciliation and event-routing paths without
a live Indigo server.

These fakes are deliberately STATEFUL — a ``states`` dict that mutates on
``updateStatesOnServer``, ``stateListOrDisplayStateIdChanged`` call counting,
``replaceOnServer`` failure/rollback semantics, and a real device-group model
(see ``FakeDeviceFactory``, per ADR-0009) — because the inbound code under
test reads state back and branches on it (e.g. "was the rebuild hook called
exactly once?", "did a failed replace roll the name back?").

Do NOT merge these with fakes.py's ``FakeIndigoDevice``/``FakeIndigoDevices``.
Those are EXPORT-side static attribute holders for a different direction of
data flow and are deliberately simpler; conflating the two families would
either weaken this stateful suite or drag unwanted state-machine behaviour
into the export-side one. Keep them in separate modules.
"""
from __future__ import annotations

# Real Indigo auto-derives these built-in states from Supports* props, both at
# device CREATION and at any later pluginProps replace. FakeDev seeds them in
# both places so handler update guards (e.g. ElectricalPowerHandler's
# `"curEnergyLevel" not in indigo_dev.states`) behave like the real server —
# issue #79's priming/live-routing tests need this true immediately after
# creation, not only after a reconcile-triggered replacePluginPropsOnServer.
_SUPPORTS_TO_STATE = {
    "SupportsPowerMeter": "curEnergyLevel",
    "SupportsEnergyMeter": "accumEnergyTotal",
    "SupportsBatteryLevel": "batteryLevel",
    "SupportsSensorValue": "sensorValue",
}

# Devices.xml declares these as plain custom states (no Supports* prop gates
# them) — real Indigo instantiates a device's declared <States> at CREATION
# regardless of props, unlike the Supports*-driven built-ins above. Only
# BooleanStateConfigHandler's guard (issue #85 — "sensitivityLevel" not in
# indigo_dev.states) needs this modelled; matterLock's lockState needs no
# guard (door_lock.py writes it unconditionally) so it needs no seeding here.
_STATIC_DEVICE_TYPE_STATES = {
    # Mirrors the <States> each type declares in Devices.xml — real Indigo
    # creates a device with every declared state present, and the handlers'
    # "is this state on this device?" guards depend on that being true.
    "matterMotionSensor": {"sensitivityLevel", "holdTime"},
    "matterContactSensor": {"sensitivityLevel"},
    "matterRelay": {"startUpOnOff"},
    # issue #204 / ADR-0008 — declared unconditionally in Devices.xml (no
    # Supports* prop gates any of them), same custom-type discipline as
    # matterEnergyMeter/matterUnknown's `reachable`. curEnergyLevel/
    # accumEnergyTotal joined the list once ep-0 energy attribution shipped
    # (issue #204's final stage) — same ids matterEnergyMeter already
    # declares, also unconditionally.
    "matterNode": {"nodeLabel", "softwareVersion", "batteryLevel", "reachable",
                   "curEnergyLevel", "accumEnergyTotal"},
}


class FakeDev:
    def __init__(self, dev_id, name, device_type_id, props, initial_states=None):
        self.id = dev_id
        self.name = name
        self.deviceTypeId = device_type_id
        self.pluginProps = props
        self.states = {}
        for prop_key, state_key in _SUPPORTS_TO_STATE.items():
            if props.get(prop_key):
                self.states[state_key] = 0  # Indigo-style initial value
        for state_key in _STATIC_DEVICE_TYPE_STATES.get(device_type_id, ()):
            self.states.setdefault(state_key, 0)
        # Callers that need states Devices.xml wouldn't auto-derive here (e.g.
        # matterButton's lastButtonEvent/pressCount) seed them explicitly.
        self.states.update(dict(initial_states or {}))
        self.error = None
        self.errorState = ""
        self.folderId = 0
        # See replaceOnServer: fail_replace models real Indigo's discard-on-
        # failure semantics; _name_on_server is the last name Indigo accepted.
        self.fail_replace = False
        self._name_on_server = name
        # Issue #190: how many times Indigo was asked to rebuild this device's
        # state list. The question the tests care about is WHEN that is asked
        # for, not what Indigo does next, so the fake only counts.
        self.state_list_rebuilds = 0
        # Real Indigo derives the list display from Supports* props at CREATION
        # and caches it (issue #56) — approximate the precedence rule verified
        # live on jarvis: a True Supports* wins; with BOTH explicitly False the
        # Devices.xml UiDisplayStateId applies ("uiDisplayState" stands in for
        # it here). Deliberately do NOT re-derive in replacePluginPropsOnServer
        # below (models the pessimistic cached case the warn path exists for).
        if props.get("SupportsSensorValue"):
            self.displayStateId = "sensorValue"
        elif "SupportsOnState" in props and not props.get("SupportsOnState"):
            self.displayStateId = "uiDisplayState"
        else:
            self.displayStateId = "onOffState"

    def updateStatesOnServer(self, kvlist):
        for kv in kvlist:
            self.states[kv["key"]] = kv["value"]
            # Real Indigo's states dict answers a "key.ui" lookup with the
            # display value from a uiValue-bearing write (used by both fix A's
            # evidence check and fix D's reachable/unreachable column text).
            if "uiValue" in kv:
                self.states[f"{kv['key']}.ui"] = kv["uiValue"]

    def stateListOrDisplayStateIdChanged(self):
        self.state_list_rebuilds += 1

    def setErrorStateOnServer(self, value):
        self.error = value
        self.errorState = value

    def replaceOnServer(self):
        # Real Indigo persists the in-memory edits (name etc.). Production
        # writes dev.name BEFORE calling this, and real Indigo discards the
        # in-memory edit on failure — so a failing fake must roll the name
        # back, or later passes vote on a name Indigo never had (issue #204
        # verification round). Set fail_replace=True to model failure; do NOT
        # override this method with a bare raiser.
        if self.fail_replace:
            self.name = self._name_on_server
            raise ValueError("replaceOnServer refused (fail_replace)")
        self.replaced = True
        self._name_on_server = self.name

    def replacePluginPropsOnServer(self, new_props):
        # Real Indigo updates pluginProps and rebuilds device states from Supports*
        # entries.  The fake merges the new props and, for each Supports* key that
        # transitions to True, seeds the corresponding state so handler guards pass.
        self.pluginProps = dict(new_props)
        self.replaced_props = True
        # Simulate Indigo auto-creating states for Supports* props.
        for prop_key, state_key in _SUPPORTS_TO_STATE.items():
            if new_props.get(prop_key) and state_key not in self.states:
                self.states[state_key] = 0  # Indigo-style initial value


class FakeFolder:
    def __init__(self, folder_id, name):
        self.id = folder_id
        self.name = name


class FakeFolderFactory:
    """Stands in for ``indigo.devices.folder`` (the folder command namespace)."""

    def __init__(self, devices):
        self.devices = devices

    def create(self, name):
        return self.devices.add_folder(name)


class FakeDevices:
    def __init__(self):
        self._by_id = {}
        self._counter = 1000
        self._folders = {}
        self._folder_counter = 0

    def next_id(self):
        self._counter += 1
        return self._counter

    def add(self, dev):
        self._by_id[dev.id] = dev

    def add_folder(self, name):
        self._folder_counter += 1
        folder = FakeFolder(self._folder_counter, name)
        self._folders[folder.id] = folder
        return folder

    @property
    def folders(self):
        return list(self._folders.values())

    def __iter__(self):
        return iter(list(self._by_id.values()))

    def __getitem__(self, dev_id):
        return self._by_id[dev_id]

    def iter(self, _filter=None):
        # Real Indigo EXCLUDES unconfigured devices from iter("self") — that
        # exclusion is the whole mechanism of issue #62, so the fake has to
        # model it or a test for the stray warning would pass vacuously.
        # Plain iteration (__iter__) stays unfiltered, like the real
        # `indigo.devices`, which is the only place a stray is still visible.
        return [dev for dev in self._by_id.values() if getattr(dev, "configured", True)]


class FakeDeviceFactory:
    """Stands in for the ``indigo.device`` command namespace.

    Since issue #204 stage 2 this carries a REAL device-group model rather than
    recording calls, and since ADR-0009 that model is the one the CONTROLLED
    EXPERIMENT on jarvis (2026-08-12) established rather than the one the docs
    imply:

    * Indigo orders a group's members by device AGE (creation order), and the
      OLDEST member is the root — ``getGroupList``'s first element, identical
      whichever member is asked.
    * ``groupWithDevice(a, b)`` and ``groupWithDevice(b, a)`` produce the
      SAME group. **The argument order does nothing.** A fake that honoured
      arg order would let a plugin that (wrongly) depends on it pass.
    * ``indigo.device.delete`` REFUSES to delete the root of a non-empty
      group — whichever device that turns out to be, which is what makes
      ``delete_node``'s dissolve-first shape load-bearing rather than
      decorative.
    """

    def __init__(self, devices):
        self.devices = devices
        self.created = []
        #: dev_id → the group's member list, SHARED by every member, ordered
        #: OLDEST FIRST; [0] is therefore the root. Absent means ungrouped.
        self.groups = {}
        #: (dev_1, dev_2) per groupWithDevice call — the idempotence assertion
        #: is "a second reconcile pass adds none of these". The order inside
        #: the tuple is what the plugin passed and means nothing to Indigo.
        self.group_calls = []
        self.ungroup_calls = []

    @staticmethod
    def _id_of(dev_or_id):
        return dev_or_id.id if hasattr(dev_or_id, "id") else int(dev_or_id)

    def _age_of(self, dev_id):
        """Creation sequence of a device — lower is older.

        ``FakeDevices.next_id`` is a monotonic counter, so an id IS its
        creation rank for every device these tests make; devices built by hand
        with an explicit id (the orphan/ghost fixtures) sort by that id, which
        is all the ordering they need.
        """
        return dev_id

    def create(self, protocol=None, deviceTypeId="", name="", props=None, folder=0, **kwargs):
        dev = FakeDev(self.devices.next_id(), name, deviceTypeId, dict(props or {}))
        if isinstance(folder, int) and folder:
            dev.folderId = folder
        self.devices.add(dev)
        self.created.append(dev)
        return dev

    def delete(self, dev):
        dev_id = self._id_of(dev)
        members = self.groups.get(dev_id)
        if members and len(members) > 1 and members[0] == dev_id:
            raise ValueError(
                "cannot delete device %s: it is the root of a non-empty device group" % dev_id)
        self._drop_from_group(dev_id)
        self.devices._by_id.pop(dev_id, None)

    def moveToFolder(self, dev_or_id, value=None):
        dev = dev_or_id if hasattr(dev_or_id, "folderId") else self.devices[dev_or_id]
        dev.folderId = value

    # -- the device-group model -----------------------------------------
    def getGroupList(self, dev_or_id):
        dev_id = self._id_of(dev_or_id)
        if dev_id not in self.devices._by_id:
            # Real Indigo cannot answer for an id that is not a device, and the
            # plugin's index CAN carry one: plugin.deviceDeleted only prunes
            # matterNode ids, so a hand-deleted endpoint device leaves a dead
            # id behind (issue #204 review, fix D). Without this tooth the
            # grouping sweep looks harmless against a fake that answers anyway.
            raise ValueError("device %s does not exist" % dev_id)
        members = self.groups.get(dev_id)
        # An ungrouped device answers with just itself — the plugin tolerates an
        # empty list too (both shapes are undocumented; see _ensure_grouped).
        return list(members) if members else [dev_id]

    def groupWithDevice(self, dev_1, dev_2):
        """The experiment's semantics: the two devices' groups are UNIONED and
        the result is ordered by device age, oldest first.

        ``groupWithDevice(motion, node)`` and ``groupWithDevice(node, motion)``
        returned byte-identical member lists on the live rig, and adding a
        third device to an existing pair left the root untouched — so this
        models a symmetric union with an age sort and no notion of a joiner.

        What is and isn't experiment-backed, honestly: SINGLETON+SINGLETON
        (both argument orders) and SINGLETON-JOINS-EXISTING-GROUP (the
        experiment's third line) are what the live rig actually exercised.
        A general GROUP+GROUP union — two already-multi-member groups
        merged in one call — is EXTRAPOLATED from those, never run on
        jarvis. Production's own guard (`_ensure_grouped`'s family check
        only ever passes a SINGLETON node device as one side) means only
        the backed directions are exercised today; this fake unions
        unconditionally because nothing here currently calls it any other
        way. Anyone relaxing that guard to call this with two genuine
        multi-member groups must extend the live experiment first, not
        just trust this model to still be right.
        """
        first, second = self._id_of(dev_1), self._id_of(dev_2)
        self.group_calls.append((first, second))
        members = set(self.groups.get(first) or [first])
        members |= set(self.groups.get(second) or [second])
        ordered = sorted(members, key=self._age_of)
        for member in ordered:
            self.groups[member] = ordered

    def ungroupDevice(self, dev_or_id):
        dev_id = self._id_of(dev_or_id)
        self.ungroup_calls.append(dev_id)
        self._drop_from_group(dev_id)

    def _drop_from_group(self, dev_id):
        members = self.groups.pop(dev_id, None)
        if not members:
            return
        remaining = [member for member in members if member != dev_id]
        for member in remaining:
            # A group of one is no group at all.
            if len(remaining) > 1:
                self.groups[member] = remaining
            else:
                self.groups.pop(member, None)
