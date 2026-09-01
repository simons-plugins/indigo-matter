"""The Matter menu's read-only diagnostics (issue #191; #334 for the third item).

Three menu items, all of which answer a question about a device without
touching it:

* **Report settable Matter settings…** — the on-demand half of the automatic
  settable-attribute report. Devices commissioned before that report shipped
  would otherwise never produce one, and a user asking the question deserves an
  answer whether or not the plugin already answered it months ago into a log
  nobody was reading.
* **Explore Matter attributes…** — a labelled dump of a chosen
  node/endpoint/cluster, plus a single live attribute read for confirming one
  value. Power-user surface; deliberately unfiltered.
* **Report Thread mesh…** — roles, neighbour/route links (RSSI/LQI/frame-error),
  the leader, foreign routers and health flags for every Thread node, parsed by
  :mod:`thread_mesh` from matter-server's ThreadNetworkDiagnostics cache and
  optionally live-refreshed first for the sleepy devices via the shared
  :func:`thread_survey.run_survey` (the same coroutine the IWS Thread page uses,
  so the live-read policy lives in one place).

**None of them writes anything, now or ever** (decided 2026-08-11 — see
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
import thread_mesh
import thread_survey
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


def _truthy(value) -> bool:
    """Indigo checkboxes arrive as bools or as "true"/"false" strings — ``bool("false")``
    is ``True``, which is the bug this exists to avoid (#334 post-review, B3.1).

    Same idiom as ``ExportDialogMixin._truthy`` (``export_dialog_mixin.py`` ~L62-71),
    replicated here rather than imported: this bare mixin has no guarantee
    ``ExportDialogMixin`` is anywhere in its MRO when instantiated on its own, which
    is exactly how ``tests/test_diagnostics_menu.py``'s ``mixin`` fixture builds it.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


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
        except Exception as exc:  # pylint: disable=broad-except
            # bookkeeping must never sink a reconcile
            #
            # Absorbing: any prefs-write failure. Safe because a failed survey
            # save costs one repeated report next start — and this hook IS the
            # logging half of SurveyLog._persist's deliberately silent
            # contract (issue #308), so the silence there is answered here.
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
        except Exception as exc:  # pylint: disable=broad-except
            # a label must never break the picker
            #
            # Absorbing: any device-name lookup failure. A MISSING device is
            # the routine case here (deleted out of band while its endpoint
            # mapping lingers), and unlike device_sync's lookups this one
            # gates NOTHING — it degrades a display label to the bridged-child
            # identity or the bare endpoint number. Raising would take the
            # dialog down, which is the only real risk on this path.
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
        except Exception as exc:  # pylint: disable=broad-except
            # surfaced to the dialog below
            #
            # Absorbing: any failure of a live device read — matter.read's
            # failure modes are open-ended (protocol refusals, a connection
            # dropping mid-await). Not swallowed: the warning names
            # cluster/attribute/node and the exception text goes into the
            # dialog's error field. Best-effort by design (ADR-0004) and
            # nothing downstream gates on it.
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
    # Deliverable C — the Thread mesh report (#334)
    # ------------------------------------------------------------------
    def menuReportThreadMesh(self, valuesDict, menuId=""):  # noqa: N802, ARG002  pylint: disable=too-many-return-statements
        """Log the Thread mesh: roles, links, the leader, foreign routers and
        health flags, from :func:`thread_survey.run_survey` + :mod:`thread_mesh`.

        ``liveReadSleepy`` (default on) live-refreshes every non-router node
        first — the same coroutine the IWS Thread page uses, so the
        cache-vs-live policy is not duplicated. A ``get_nodes()`` failure is a
        failed call (routed through the same "matter-server unavailable" shape
        as every other diagnostic here), never printed as an empty mesh — and
        (#334 post-review, B2.4) neither is a fabric that reported raw nodes
        this module could not even address (``Survey.skipped``): only a fabric
        that genuinely has no Thread devices gets the friendly answer.
        """
        errors = indigo.Dict()
        # Same guard, same wording, as HttpApiMixin._thread_mesh_snapshot
        # (#334 post-review, B5.6) — this menu used to skip the `.connected`
        # check and could hang against a runtime that exists but is not
        # actually talking to matter-server yet.
        if self.runtime is None or self.matter is None or not self.matter.connected:
            errors["liveReadSleepy"] = "The plugin is not connected to matter-server yet."
            return (False, valuesDict, errors)

        # #334 post-review, B3.1: Indigo delivers a checkbox as the STRING
        # "true"/"false", and bool("false") is True — this used to always
        # live-read regardless of the checkbox, silently ignoring "off".
        live_sleepy = _truthy(valuesDict.get("liveReadSleepy", True))
        try:
            survey = thread_survey.run_survey(
                self.runtime, self.matter, live_sleepy=live_sleepy,
                node_names=self._thread_node_names(),  # B5.4 — same names the page uses
            )
        except FuturesTimeoutError:
            # #334 post-review, B2.7: name the timeout on the Event Log — the
            # dialog error alone told the user to "see the Event Log" for
            # detail that was never actually written there.
            self.logger.warning(
                "Matter: Thread mesh survey timed out after %.0f s (live=%s)",
                ATTRIBUTE_TIMEOUT + SURVEY_READ_TIMEOUT, live_sleepy)
            errors["liveReadSleepy"] = "matter-server did not answer in time — see the Event Log."
            return (False, valuesDict, errors)
        except Exception as exc:  # pylint: disable=broad-except
            # get_nodes() failing outright is a failed call, not an empty mesh
            # (root workspace CLAUDE.md degradation-path convention) — surfaced
            # as a dialog error rather than printed as "no Thread devices".
            self.logger.warning("Matter: could not read the Thread mesh: %s", exc)
            errors["liveReadSleepy"] = f"matter-server could not be read: {exc}"
            return (False, valuesDict, errors)

        # #334 post-review, B2.4: three genuinely different "nothing to show"
        # situations, told apart rather than all printed as one friendly
        # empty mesh (root workspace CLAUDE.md degradation-path convention —
        # an unusable precondition is a failed call, not an empty result).
        if survey.raw_count == 0:
            errors["liveReadSleepy"] = "matter-server reports no commissioned nodes at all."
            return (False, valuesDict, errors)
        if not survey.diags and survey.skipped:
            errors["liveReadSleepy"] = (
                f"{len(survey.skipped)} raw node(s) could not be read: {'; '.join(survey.skipped)}")
            return (False, valuesDict, errors)
        if not survey.diags:
            self.logger.info("Matter: no Thread devices are commissioned yet.")
            return (True, valuesDict)

        mesh = thread_mesh.build_mesh(survey.diags)
        for line in thread_mesh.render_report(mesh, survey.diags):
            self.logger.info("%s", line)

        if mesh.unreadable:
            # Partial failure, same convention as menuReportSettings: still
            # print everything (the cached values above), then say so — a
            # diagnostic that looks complete when it is not is the failure
            # mode a diagnostic can least afford.
            errors["liveReadSleepy"] = (
                f"{len(mesh.unreadable)} node(s) could not be live-read — cached values "
                f"shown; see the Event Log.")
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def _thread_node_names(self) -> dict:
        """``node_id -> the single name a human would use for it``, shared by
        this menu and :meth:`http_api_mixin.HttpApiMixin.http_thread_page`
        (cross-mixin ``self.`` call, the established pattern issue #146 set —
        ``Plugin`` composes both) so a node's name can never disagree between
        the two surfaces (#334 post-review, B5.4).

        Prefers the ``matterNode`` device's own name (ADR-0008: one Indigo
        device per node) over :meth:`device_sync.DeviceSync.list_nodes`'s
        comma-joined list of EVERY endpoint device's name — a 4-endpoint FP300
        used to show up as "FP300 Motion, FP300 Illuminance, FP300
        Temperature, FP300 Humidity" instead of just "FP300". Falls back to
        that comma-joined list for a node with no ``matterNode`` device yet
        (ADR-0008's AttributeList gate not cleared) — same as before this
        change for that case, just no longer the first choice.

        A node with no Indigo devices at all (neither a ``matterNode`` device
        nor any endpoint device) is simply absent from this returned dict
        (#334 finding B4.15) — :func:`thread_survey._node_name` then supplies
        its own fallback ("<vendor> <product>" from BasicInformation, or
        "node 0x.." with neither), which this function has no need to
        duplicate.
        """
        if self.device_sync is None:
            return {}
        try:
            names: dict[int, str] = {}
            for node_id, endpoint_names in self.device_sync.list_nodes():
                node_dev_id = self.device_sync.lookup(node_id, 0, device_type_id="matterNode")
                if node_dev_id is not None:
                    try:
                        names[node_id] = indigo.devices[node_dev_id].name
                        continue
                    except Exception:  # pylint: disable=broad-except
                        pass  # fall through to the endpoint-name join below
                if endpoint_names:
                    names[node_id] = ", ".join(endpoint_names)
            return names
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: this is cosmetic naming only, never worth failing the
            # report/page over — thread_survey degrades a missing name to
            # "<vendor> <product>" or "node 0x.." on its own.
            self.logger.debug("thread node names: could not resolve (%s)", exc)
            return {}

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
        except Exception as exc:  # reported to the caller as unavailable
            raise MatterUnavailable(str(exc)) from exc
        if not raw:
            return None
        try:
            return parse_node(raw)
        except Exception as exc:
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
