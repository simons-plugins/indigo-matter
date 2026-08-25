"""The Matter menu's read-only diagnostics (issue #191).

Two menu items, both of which answer "what does this device actually expose?"
without touching it:

* **Report settable Matter settings…** — the on-demand half of the automatic
  settable-attribute report. Devices commissioned before that report shipped
  would otherwise never produce one, and a user asking the question deserves an
  answer whether or not the plugin already answered it months ago into a log
  nobody was reading.
* **Explore Matter attributes…** — a labelled dump of a chosen
  node/endpoint/cluster, plus a single live attribute read for confirming one
  value. Power-user surface; deliberately unfiltered.

**Neither writes anything, now or ever** (decided 2026-08-11 — see
:mod:`settings_report` for the three reasons). If a write is needed the answer
is a new ``DeviceSetting`` declaration, which is bounds-checked and verified by
read-back; not a generic writer, which is neither.

**Everything reports into the event log.** Indigo's ConfigUI has no per-field
change callback — the only dynamic mechanism, ``<List dynamicReload="true">``,
regenerates a list's *options* — so a dialog physically cannot display a result.
Same constraint, and the same answer, as the pairing codes in PRD §3.8.

**Why a mixin rather than functions.** Indigo resolves every ``<CallbackMethod>``
and list method by name as an attribute on the ``Plugin`` class, so callbacks
have to be class members (issue #146). Like its siblings this module defines no
``__init__``/``deviceUpdated``/etc., or the ``super()`` chain to
``indigo.PluginBase`` would break.
"""
from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Optional

import indigo  # provided by the Indigo runtime

import settings_report
from matter_client import ATTRIBUTE_TIMEOUT
from matter_handlers.writable_attributes import MODEL_VERSION
from matter_model import node_id_to_str, parse_node
from plugin_constants import (
    ALL_OPTION_ID,
    NO_SELECTION_ID,
    SURVEY_LOG_PREF,
    SURVEY_READ_TIMEOUT,
    degrades_to_list_error,
)


class MatterUnavailable(Exception):
    """matter-server could not answer. Re-declared rather than imported from
    ``http_api_mixin`` — a mixin importing a sibling mixin is the back-import
    issue #146 removed, and ``Plugin`` composes both anyway."""


class DiagnosticsMenuMixin:
    """Read-only Matter diagnostics: the settable-attribute report and explorer."""

    # ------------------------------------------------------------------
    # Prefs
    # ------------------------------------------------------------------
    def _save_survey_log(self, blob: str) -> None:
        """Persist the "already reported" log (issue #191).

        Written through ``self.pluginPrefs`` at call time rather than a captured
        mapping: Indigo rebinds it when the PluginConfig dialog is saved, and a
        held reference would write into an orphan. ``savePluginPrefs`` is the
        commit — without it the value survives only until the plugin stops.

        Caught and logged rather than left to escape into
        ``SurveyLog._persist`` (issue #308): that method is deliberately
        silent on the assumption that "the caller supplies the save hook and
        logs there if it wants to" — this is that logging. A failure here is
        cosmetic (the affected device(s) are simply re-reported on the next
        start), which is why WARNING rather than anything louder.
        """
        try:
            self.pluginPrefs[SURVEY_LOG_PREF] = blob
            indigo.server.savePluginPrefs()
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never sink a reconcile
            self.logger.warning(
                "Matter: could not save the settable-attribute survey log (%s) — "
                "the affected device(s) will be re-reported on the next start.", exc)

    # ------------------------------------------------------------------
    # Pickers
    # ------------------------------------------------------------------
    def getSurveyNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Node picker for the report, with an "every device" row first.

        Built from ``list_nodes()``, the same source the decommission picker
        uses, so a node with no Indigo devices at all — an empty bridge, the
        thing most in need of a survey — is still listed.
        """
        options = [(ALL_OPTION_ID, "Every Matter device")]
        options.extend(self._matter_node_options())
        return options

    def getExploreNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Node picker for the explorer. No "every device" row: a full dump of
        every node at once is thousands of log lines and answers no question
        anyone actually has."""
        options = self._matter_node_options()
        return options or [(NO_SELECTION_ID, "(no Matter devices — none commissioned yet)")]

    @degrades_to_list_error
    def _matter_node_options(self) -> list:
        if self.device_sync is None:
            return []
        return [
            (str(node_id), f"{', '.join(names) if names else '(no Indigo devices)'} "
                           f"— node {node_id_to_str(node_id)}")
            for node_id, names in self.device_sync.list_nodes()
        ]

    def exploreNodeChanged(self, valuesDict=None, typeId="", devId=0):  # noqa: N802, ARG002
        """No-op callback on the explorer's node menu.

        It exists purely to make Indigo round-trip to the plugin, which is what
        re-runs every ``dynamicReload`` list in the dialog — the endpoint and
        cluster pickers below. There is no other mechanism: ConfigUI has no
        per-field change event, so a menu's ``<CallbackMethod>`` is the whole
        cascade. Returning the dict unchanged is the documented shape.
        """
        return valuesDict

    def getExploreEndpoints(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Endpoints of the picked node, from its cached snapshot.

        A wildcard row is offered because a bridge's endpoints are the whole
        point of exploring it — issue #191 constraint 2: addressing here is
        (node, endpoint, cluster, attribute), not just an attribute.
        """
        node = self._explore_node(valuesDict)
        options = [(ALL_OPTION_ID, "All endpoints")]
        if node is None:
            return options
        options.extend((str(ep.endpoint_id), self._endpoint_label(node, ep))
                       for ep in node.endpoints)
        return options

    def _endpoint_label(self, node, endpoint) -> str:
        """Name an endpoint by its Indigo device when it has one.

        An endpoint NUMBER means nothing to a user; "Kitchen Motion" does. Falls
        back to the bridged-child identity, then to the bare number.
        """
        try:
            dev_id = self.device_sync.lookup(node.node_id, endpoint.endpoint_id)
            if dev_id:
                return f"{indigo.devices[dev_id].name} (endpoint {endpoint.endpoint_id})"
        except Exception as exc:  # noqa: BLE001 - a label must never break the picker
            self.logger.debug("explore: label for endpoint %s failed: %s",
                              endpoint.endpoint_id, exc)
        label = endpoint.node_label or endpoint.product_name
        return f"{label} (endpoint {endpoint.endpoint_id})" if label \
            else f"Endpoint {endpoint.endpoint_id}"

    def getExploreClusters(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Clusters present on the picked node (narrowed to the picked endpoint).

        Only clusters the device actually reports — offering all 135 the model
        knows about would bury the eight this device has.
        """
        node = self._explore_node(valuesDict)
        options = [(ALL_OPTION_ID, "All clusters")]
        if node is None:
            return options
        endpoint = _picked_int(valuesDict, "endpoint")
        clusters = sorted({
            cluster for (ep, cluster, _attr) in node.attributes
            if endpoint is None or ep == endpoint
        })
        options.extend((str(c), settings_report.cluster_label(c)) for c in clusters)
        return options

    # ------------------------------------------------------------------
    # Deliverable B — the on-demand settable-attribute report
    # ------------------------------------------------------------------
    def menuReportSettings(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Log the settable-attribute report for one node, or for all of them.

        Always ``force``: the automatic report is once per device by design, and
        a user who chose this menu item is asking now.
        """
        errors = indigo.Dict()
        selected = str(valuesDict.get("node", "") or "")
        if not selected or selected == NO_SELECTION_ID:
            errors["node"] = "Choose a device (or 'Every Matter device')."
            return (False, valuesDict, errors)

        if selected == ALL_OPTION_ID:
            node_ids = [node_id for node_id, _names in self.device_sync.list_nodes()]
            if not node_ids:
                errors["node"] = "No Matter devices are commissioned yet."
                return (False, valuesDict, errors)
        else:
            try:
                node_ids = [int(selected)]
            except (TypeError, ValueError):
                errors["node"] = "Invalid selection."
                return (False, valuesDict, errors)

        self.logger.info(
            "Matter settable-attribute report (Matter data model %s) — %d device(s). "
            "This is information, not a fault.", MODEL_VERSION, len(node_ids))
        failures = []
        for node_id in node_ids:
            try:
                node = self._fetch_node(node_id)
            except MatterUnavailable as exc:
                failures.append(f"{node_id_to_str(node_id)} ({exc})")
                continue
            if node is None:
                failures.append(f"{node_id_to_str(node_id)} (matter-server does not know it)")
                continue
            self.device_sync.report_settable_attributes(node, force=True)
        if failures:
            # Reported as a dialog error, not just a log line: a partial answer
            # that looks complete is the failure mode a diagnostic can least
            # afford — the whole point is that the user trusts what it prints.
            self.logger.warning("could not survey: %s", "; ".join(failures))
            errors["node"] = (f"{len(failures)} device(s) could not be read — see the "
                              f"Event Log. The rest were reported.")
            return (False, valuesDict, errors)
        return (True, valuesDict)

    # ------------------------------------------------------------------
    # Deliverable A — the read-only explorer
    # ------------------------------------------------------------------
    def menuExploreAttributes(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Dump what a node holds, or read one attribute live. Never writes."""
        errors = indigo.Dict()
        selected = str(valuesDict.get("node", "") or "")
        if not selected or selected in (NO_SELECTION_ID, ALL_OPTION_ID):
            errors["node"] = "Choose a Matter device."
            return (False, valuesDict, errors)
        try:
            node_id = int(selected)
        except (TypeError, ValueError):
            errors["node"] = "Invalid selection."
            return (False, valuesDict, errors)

        endpoint = _picked_int(valuesDict, "endpoint")
        cluster = _picked_int(valuesDict, "cluster")
        live = str(valuesDict.get("attribute", "") or "").strip()

        if live:
            return self._read_one_live(valuesDict, errors, node_id, endpoint, cluster, live)

        try:
            node = self._fetch_node(node_id)
        except MatterUnavailable as exc:
            errors["node"] = f"matter-server could not be read: {exc}"
            return (False, valuesDict, errors)
        if node is None:
            errors["node"] = "matter-server does not know that node."
            return (False, valuesDict, errors)
        for line in settings_report.explore_lines(node, endpoint, cluster):
            self.logger.info("%s", line)
        return (True, valuesDict)

    def _read_one_live(self, valuesDict, errors, node_id, endpoint, cluster, raw_attribute):
        """The one LIVE read the explorer offers — the counterpart to the dump.

        Separate from the dump on purpose. The dump comes from matter-server's
        cache, which for an unsubscribed attribute can be hours stale; this goes
        to the device. Both are useful and they are not interchangeable, so the
        UI makes the user say which one they want rather than quietly picking.

        Needs a concrete endpoint and cluster: a live read is one attribute at
        one address, and there is no wildcard form of it. Refusing is better than
        fanning out into a read storm against a sleepy device.
        """
        try:
            attribute = int(raw_attribute, 0)
        except (TypeError, ValueError):
            errors["attribute"] = "Enter an attribute id, e.g. 0x4003 or 16387."
            return (False, valuesDict, errors)
        if endpoint is None:
            errors["endpoint"] = "Choose one endpoint — a live read needs an exact address."
            return (False, valuesDict, errors)
        if cluster is None:
            errors["cluster"] = "Choose one cluster — a live read needs an exact address."
            return (False, valuesDict, errors)
        if self.runtime is None or self.matter is None:
            errors["attribute"] = "The plugin is not connected to matter-server yet."
            return (False, valuesDict, errors)
        try:
            value = self.runtime.submit(
                self.matter.read(node_id, endpoint, cluster, attribute)
            ).result(timeout=ATTRIBUTE_TIMEOUT + SURVEY_READ_TIMEOUT)
        except FuturesTimeoutError:
            # A sleepy Thread device that missed its poll window looks exactly
            # like one that does not implement the attribute, and saying so is
            # the difference between a diagnostic and a guess.
            errors["attribute"] = ("No answer within the timeout. A battery/Thread device "
                                   "may simply have been asleep — try again.")
            return (False, valuesDict, errors)
        except Exception as exc:  # noqa: BLE001 - surfaced to the dialog below
            self.logger.warning("live read of %s on node %s failed: %s",
                                settings_report.attribute_label(cluster, attribute),
                                node_id_to_str(node_id), exc)
            errors["attribute"] = f"The read failed: {exc}"
            return (False, valuesDict, errors)
        self.logger.info(
            "Live read — node %s endpoint %s %s %s = %s",
            node_id_to_str(node_id), endpoint,
            settings_report.cluster_label(cluster),
            settings_report.attribute_label(cluster, attribute),
            settings_report.render_value(value))
        return (True, valuesDict)

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------
    def _explore_node(self, valuesDict) -> Optional[Any]:
        """The picked node's snapshot, for the endpoint/cluster pickers.

        Returns None — leaving the picker with only its wildcard row — rather
        than raising, because these run while the dialog is being drawn and a
        list callback that throws takes the dialog with it.

        This is the one place a picker reaches matter-server, which is why the
        wildcard row is always first: the dialog stays usable when it cannot.
        """
        if valuesDict is None:
            return None
        selected = str(valuesDict.get("node", "") or "")
        if not selected or selected in (NO_SELECTION_ID, ALL_OPTION_ID):
            return None
        try:
            return self._fetch_node(int(selected))
        except (TypeError, ValueError, MatterUnavailable) as exc:
            self.logger.debug("explore: node %s unavailable for the picker: %s", selected, exc)
            return None

    def _fetch_node(self, node_id: int):
        """One node's snapshot as a :class:`~matter_model.NodeInfo`, or None.

        ``get_node`` is matter-server's **cache** — complete and instant, with no
        round trip to the device. For exploration that is the right source and
        the only affordable one: a live per-attribute sweep of a sleepy Thread
        device would take minutes and hold the Indigo UI thread for all of them.
        A stale reading is a real cost, which is what the live single read and
        the header on every dump are for.
        """
        if self.runtime is None or self.matter is None:
            raise MatterUnavailable("the plugin is not connected to matter-server yet")
        try:
            raw = self.runtime.submit(
                self.matter.get_node(int(node_id))).result(timeout=SURVEY_READ_TIMEOUT)
        except FuturesTimeoutError as exc:
            raise MatterUnavailable("matter-server did not answer in time") from exc
        except Exception as exc:  # noqa: BLE001 - reported to the caller as unavailable
            raise MatterUnavailable(str(exc)) from exc
        if not raw:
            return None
        try:
            return parse_node(raw)
        except Exception as exc:  # noqa: BLE001
            raise MatterUnavailable(f"the node's details could not be parsed: {exc}") from exc


def _picked_int(valuesDict, key: str) -> Optional[int]:
    """A picker's value as an int, or None for the wildcard/unset row.

    ``NO_SELECTION_ID`` is deliberately NOT treated as a wildcard here, unlike
    everywhere else it appears: it is the string ``"0"``, and **endpoint 0 is a
    real Matter endpoint** — the root node, which carries BasicInformation and
    every other node-wide cluster, and is therefore one of the more interesting
    things to explore. Folding it into "all endpoints" would make the root the
    one address the explorer could not be pointed at. These two pickers never
    emit the sentinel anyway; only ``ALL_OPTION_ID`` means all.
    """
    if valuesDict is None:
        return None
    raw = str(valuesDict.get(key, "") or "")
    if not raw or raw == ALL_OPTION_ID:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
