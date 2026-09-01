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
  :func:`thread_survey.survey_thread_nodes` (the same coroutine the IWS Thread
  page's :func:`thread_survey.run_survey` uses, so the live-read policy lives
  in one place). **Runs in the background** (#339): a live-read survey can
  take up to ~50 s, which is too long to block a menu dialog on (field-reported
  against v2026.30.0), so the menu returns immediately and the report follows
  in the Event Log via the submitted coroutine's done-callback.

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

import asyncio
import functools
from concurrent.futures import CancelledError as FuturesCancelledError
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

    #: R5 in-flight guard for the backgrounded Thread mesh survey (#339 review):
    #: the pending survey's ``concurrent.futures.Future``, or ``None``. A
    #: class-level default rather than something set in ``__init__`` — this
    #: mixin defines no ``__init__`` (see module docstring, issue #146) —
    #: assigning ``self._thread_mesh_survey_pending = ...`` always creates a
    #: same-named INSTANCE attribute, it never mutates this one.
    _thread_mesh_survey_pending: Optional[Any] = None

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
    def menuReportThreadMesh(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Start the Thread mesh survey in the BACKGROUND and return at once.

        Used to run :func:`thread_survey.run_survey` synchronously and block
        the dialog on it. A real dialog timeout was field-reported against
        v2026.30.0 on 2026-09-01: with ``liveReadSleepy`` on, the survey can
        legitimately take up to ``ATTRIBUTE_TIMEOUT + SURVEY_READ_TIMEOUT``
        (~50 s, one ``per_node_timeout`` budget for the slowest sleepy node
        plus the ``get_nodes()``/marshalling margin) — and an Indigo menu
        dialog must not block anywhere near that long. The fix moves the wait
        off the calling (Indigo) thread entirely: the survey coroutine is
        submitted to the async runtime with :meth:`AsyncRuntime.submit
        <async_runtime.AsyncRuntime.submit>` and this method returns
        immediately; :meth:`_on_thread_mesh_survey_done`, attached as the
        submitted future's done-callback, does the reporting once the survey
        actually finishes.

        **Every failure that used to come back as a dialog error now reports
        to the Event Log instead** (via ``_on_thread_mesh_survey_done`` ->
        :meth:`_log_thread_mesh_survey_result`), because the dialog is already
        closed by the time any of them could fire — a timeout, a
        ``get_nodes()`` failure, an unaddressable fabric, a genuinely empty
        (Wi-Fi-only) fabric, a partial live-read failure, and a shutdown-time
        cancellation all keep the exact wording they had as dialog errors (or,
        for cancellation, the wording the equivalent dialog error would have
        had); only the destination changed. The things that stay dialog
        errors are the two precondition checks below — not connected yet, and
        a survey already running — both still checked, and still refused,
        before anything is ever submitted.

        **``node_names`` and ``page_url`` are resolved HERE, on the Indigo
        thread, eagerly** (#339 review, R8) — not inside the coroutine, and
        not inside the done-callback. Three reasons: it restores
        ``list_nodes()``'s documented "names resolved off the dispatch
        thread" intent (resolving them inside the coroutine would move that
        work onto the loop thread instead); it keeps the plugin's first
        ``indigo.server.*`` RPC (``getWebServerURL``, inside
        ``_iws_page_url``) off the loop thread; and it means the done-callback
        touches nothing but ``self.logger`` and these two captured, already-
        resolved values — see :meth:`_on_thread_mesh_survey_done`.
        """
        errors = indigo.Dict()
        # Same guard, same wording, as HttpApiMixin._thread_mesh_snapshot
        # (#334 post-review, B5.6) — this menu used to skip the `.connected`
        # check and could hang against a runtime that exists but is not
        # actually talking to matter-server yet.
        if self.runtime is None or self.matter is None or not self.matter.connected:
            errors["liveReadSleepy"] = "The plugin is not connected to matter-server yet."
            return (False, valuesDict, errors)

        # R5 (#339 review): refuse a second concurrent survey politely rather
        # than starting one — duplicate radio traffic to sleepy battery
        # devices and a doubled report are real costs, and the guard is six
        # lines. Cleared in _on_thread_mesh_survey_done's outer `finally`, so
        # completion, failure, AND cancellation all release it.
        if self._thread_mesh_survey_pending is not None:
            errors["liveReadSleepy"] = ("A Thread mesh survey is already running — its "
                                        "report will appear in the Event Log shortly.")
            return (False, valuesDict, errors)

        # #334 post-review, B3.1: Indigo delivers a checkbox as the STRING
        # "true"/"false", and bool("false") is True — this used to always
        # live-read regardless of the checkbox, silently ignoring "off".
        live_sleepy = _truthy(valuesDict.get("liveReadSleepy", True))
        total_timeout = ATTRIBUTE_TIMEOUT + SURVEY_READ_TIMEOUT

        # R8 — eager, Indigo-thread resolution (see docstring above).
        node_names = self._thread_node_names()  # B5.4 — same names the page uses
        try:
            page_url = self._iws_page_url("thread")  # pylint: disable=no-member  # HttpApiMixin
        except Exception:  # pylint: disable=broad-except
            # R3/R8 (#339 review): a broken URL resolution must not cost the
            # WHOLE completed report (root workspace CLAUDE.md isolation
            # clause) — _log_thread_mesh_survey_result already treats
            # page_url=None as "omit the footer line", the same contract
            # render_report uses when _iws_page_url itself returns None.
            page_url = None

        async def _survey_within_budget():
            # thread_survey.run_survey enforces its budget with
            # future.result(timeout=...) on the calling (Indigo) thread —
            # there is no such thread left blocked on this call any more, so
            # the same total budget is enforced from INSIDE the coroutine
            # instead, via asyncio.wait_for (#339).
            return await asyncio.wait_for(
                thread_survey.survey_thread_nodes(
                    self.matter, live_sleepy=live_sleepy, node_names=node_names,
                ),
                timeout=total_timeout,
            )

        coro = _survey_within_budget()
        try:
            future = self.runtime.submit(coro)
        except RuntimeError:
            # R7/R4 (#339 review): every other submit() call site in these
            # mixins is guarded; this was the only bare one. A stopped loop
            # (shutdown mid-menu-click) raised this straight into Indigo's
            # menu machinery instead of the dialog-error family every other
            # "not connected yet" path uses.
            coro.close()  # avoid a "coroutine was never awaited" warning
            errors["liveReadSleepy"] = "The plugin is not connected to matter-server yet."
            return (False, valuesDict, errors)

        # Set BEFORE add_done_callback: an already-done future (as every test
        # using a synchronous fake runtime produces) invokes its done-callback
        # synchronously, inline, from inside add_done_callback() itself — the
        # pending flag has to already be in place for that callback's own
        # `finally` to have something to clear.
        self._thread_mesh_survey_pending = future
        future.add_done_callback(
            functools.partial(self._on_thread_mesh_survey_done, live_sleepy, page_url))

        self.logger.info(
            "Matter: Thread mesh survey started (live-read sleepy: %s) — the report will "
            "follow in the Event Log within about a minute.", "yes" if live_sleepy else "no")
        return (True, valuesDict)

    def _on_thread_mesh_survey_done(self, live_sleepy: bool, page_url: Optional[str],
                                     future) -> None:
        """Done-callback for the backgrounded Thread mesh survey (#339).

        Usually runs on the asyncio LOOP thread: a ``concurrent.futures.Future``
        fires its done-callbacks wherever ``set_result``/``set_exception``/
        ``cancel`` was called, and for a future returned by
        :meth:`AsyncRuntime.submit <async_runtime.AsyncRuntime.submit>` that is
        ordinarily the loop thread. **But not always** (#339 review, R6/R8):
        if the future is *already done* by the time
        ``menuReportThreadMesh`` calls ``add_done_callback`` on it, this fires
        synchronously, inline, on whatever thread made that call — Indigo's,
        in production; the test thread, for every test that uses a
        synchronous fake runtime.

        That uncertainty is exactly why the body below, and
        :meth:`_log_thread_mesh_survey_result`, touch nothing but
        ``self.logger`` and the two values captured here — ``live_sleepy`` and
        ``page_url``, both resolved eagerly on the Indigo thread by the caller
        (R8) — plus pure computation over ``future.result()``. No Indigo
        device writes, and no fresh Indigo RPCs, belong here: there is no
        thread this method can safely assume it is on.

        The whole body sits behind this one try/except, which only logs:
        asyncio swallows an exception raised out of a done-callback into its
        own default exception handler, never Indigo's Event Log, so a bug in
        the reporting logic below would otherwise vanish silently — exactly
        the "the report will follow in the Event Log" promise the synchronous
        half of this menu item just made, broken with no trace. The catch is
        deliberately ``(Exception, asyncio.CancelledError)``, not a bare
        ``BaseException`` — the latter is not this handler's job to widen to
        (``asyncio.CancelledError`` is listed explicitly only because it is a
        ``BaseException`` subclass that can legitimately arrive here via the
        synchronous already-done path above; see R1-CORRECTION).
        """
        try:
            self._log_thread_mesh_survey_result(live_sleepy, page_url, future)
        except (Exception, asyncio.CancelledError) as exc:  # pylint: disable=broad-except
            # Absorbing: anything the reporting logic below could raise — see
            # this method's docstring for why nothing may escape a
            # done-callback here. logger.exception (not .warning): this catch
            # exists for UNKNOWN bugs, and the traceback is the entire
            # diagnostic value of that (workspace CLAUDE.md standard).
            self.logger.exception(
                "Matter: Thread mesh survey report failed unexpectedly: %s", exc)
        finally:
            # R5: release the in-flight guard on every path out of the
            # survey — completion, failure, AND cancellation alike.
            self._thread_mesh_survey_pending = None

    def _log_thread_mesh_survey_result(self, live_sleepy: bool, page_url: Optional[str],
                                        future) -> None:
        """The post-survey handling ``menuReportThreadMesh`` used to do
        synchronously and report as a dialog error (#334) — now run from
        :meth:`_on_thread_mesh_survey_done` once the backgrounded survey
        actually finishes, and reported to the Event Log instead, because the
        dialog is long gone by then (#339). Every branch keeps the exact
        wording it had as a dialog error; only the destination changed.

        ``page_url`` arrives already resolved by the caller (R3/R8) — this
        method never calls ``_iws_page_url`` itself, and treats ``None`` as
        "omit the footer", the same contract :func:`thread_mesh.render_report`
        already has for it.
        """
        if future.cancelled():
            # R1-CORRECTION: a survey cancelled by plugin shutdown/restart
            # (AsyncRuntime._drain_and_close cancels every pending task) is
            # expected, not a fault — info, not a warning. Checked before
            # calling .result(): that call would raise
            # concurrent.futures.CancelledError, whose str() is EMPTY, which
            # is what used to make this fall into the generic handler below
            # and log a cause-less "could not read the Thread mesh: ".
            self.logger.info(
                "Matter: Thread mesh survey cancelled before it finished (the "
                "plugin is stopping or restarting) — no report will follow; "
                "run it again.")
            return
        try:
            survey = future.result()
        except (asyncio.CancelledError, FuturesCancelledError):
            # Belt-and-braces (R1-CORRECTION): the future.cancelled() check
            # above is the normal path for a cancelled
            # concurrent.futures.Future; this covers the same event surfacing
            # as one of these two exceptions from result() directly —
            # ``asyncio.CancelledError`` is the BaseException spelling that
            # can arrive via the synchronous already-done callback path (see
            # this method's caller's docstring).
            self.logger.info(
                "Matter: Thread mesh survey cancelled before it finished (the "
                "plugin is stopping or restarting) — no report will follow; "
                "run it again.")
            return
        except (asyncio.TimeoutError, FuturesTimeoutError):
            # #334 post-review, B2.7's reasoning still applies: name the
            # timeout on the Event Log rather than leaving the promised
            # report to simply never show up.
            self.logger.warning(
                "Matter: Thread mesh survey timed out after %.0f s (live=%s)",
                ATTRIBUTE_TIMEOUT + SURVEY_READ_TIMEOUT, live_sleepy)
            return
        except Exception as exc:  # pylint: disable=broad-except
            # get_nodes() failing outright is a failed call, not an empty mesh
            # (root workspace CLAUDE.md degradation-path convention) — reported
            # to the Event Log rather than printed as "no Thread devices".
            # This branch exists for OPEN-ENDED, expected network failures
            # (matter-server dropping, a protocol refusal) — full
            # logger.exception() would be noisy for those, so the warning
            # keeps its plain wording and a DEBUG companion carries the
            # traceback for the times it turns out not to be routine.
            self.logger.warning("Matter: could not read the Thread mesh: %s", exc)
            self.logger.debug(
                "Matter: Thread mesh survey read failure, full traceback", exc_info=True)
            return

        # #334 post-review, B2.4: three genuinely different "nothing to show"
        # situations, told apart rather than all printed as one friendly
        # empty mesh (root workspace CLAUDE.md degradation-path convention —
        # an unusable precondition is a failed call, not an empty result).
        if survey.raw_count == 0:
            self.logger.warning("Matter: matter-server reports no commissioned nodes at all.")
            return
        if not survey.diags and survey.skipped:
            self.logger.warning(
                "Matter: %d raw node(s) could not be read: %s",
                len(survey.skipped), "; ".join(survey.skipped))
            return
        if not survey.diags:
            self.logger.info("Matter: no Thread devices are commissioned yet.")
            return

        mesh = thread_mesh.build_mesh(survey.diags)
        for line in thread_mesh.render_report(mesh, survey.diags, page_url=page_url):
            self.logger.info("%s", line)

        if mesh.unreadable:
            # Partial failure, same convention as menuReportSettings: still
            # print everything (the cached values above), then say so — a
            # diagnostic that looks complete when it is not is the failure
            # mode a diagnostic can least afford.
            self.logger.warning(
                "Matter: %d node(s) could not be live-read — cached values shown above.",
                len(mesh.unreadable))

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
