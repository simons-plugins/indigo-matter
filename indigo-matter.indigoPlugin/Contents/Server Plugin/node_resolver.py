"""Node/npm toolchain resolution for :mod:`launch_agent`.

Extracted out of :class:`~launch_agent.LaunchAgent` (see that module's docstring
for the six concerns it splits into). This is the one band worth splitting on
its own: it has **zero** outbound calls into the rest of ``LaunchAgent``, only
**three** inbound call sites (``LaunchAgent.__init__``, ``install()``,
``ensure_installed()``), and only **one** attribute (``node_path``) that the
rest of the class also needs. The other five bands (plist authoring, process
control, port/orphan reaping, bootstrap-verification, install/uninstall) stay
together in ``launch_agent.py`` — they were measured at 48 cross-band calls,
mostly mutually dependent (``plist`` and ``proc`` alone call each other 10
times), so composing them out would only add indirection without reducing the
coupling.

Resolves ``npx``/``node`` (Homebrew, nvm, an explicit ``nodeBinDir`` pin, or
bare PATH), validates a pin actually runs a new-enough node (issue #101), and
tracks the node version a package was installed with so a later mismatch can
be flagged as advisory (never fatal) rather than crash-looping silently.

Must not import ``plugin.py``, any mixin, or matter.js anything (workspace
ADR-0006) — this module is reached from both agents long before either's
Matter-specific code runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Callable, Optional

NPX_CANDIDATES = ("/opt/homebrew/bin/npx", "/usr/local/bin/npx")

# matter-server 1.2.2's package.json declares engines node >= 22.13.0. npm's engines
# check is advisory by default (exits 0 on an older node), so install() gates on this
# itself — otherwise a too-old node "successfully" installs an unrunnable server.
# Shared by every agent for the same reason INSTALL_NODE_STAMP is shared: they all run
# on the one node this plugin resolved.
MIN_NODE_VERSION = (22, 13)

#: Seconds to wait for a ``node --version`` probe. Small because the probe runs
#: on the plugin startup path and node answers in milliseconds; generous enough
#: that a busy Mac is not mistaken for a wedged binary.
NODE_PROBE_TIMEOUT = 10
# Records the node version the package was installed with, so preflight can catch an
# install-node vs run-node mismatch (native-binding ABI crash) before it crash-loops.
# DELIBERATELY SHARED between agents (not per-label): project_dir holds ONE node_modules
# installed by ONE node, and every agent's LaunchAgent runs that same node. A per-agent
# stamp would claim they can diverge, which is exactly the ABI crash this guards against.
# CAVEAT the sharing does not cover, REVIEWED AT E7 AND KEPT: if nodeBinDir is repointed
# BETWEEN two agents' installs, the later install rewrites the stamp with the new node and
# the earlier agent's already-built native bindings go unwarned (npm install of package B
# does not rebuild package A's bindings). Kept shared because the alternative — per-package
# versions inside one stamp — would make `abi_warning` claim the two agents CAN legitimately
# run on different nodes, which is the opposite of true: both LaunchAgents run whatever
# single node this plugin resolved, so a per-package stamp that disagreed with the other
# would be describing a state that cannot exist. The residual risk is one stale ADVISORY
# warning after a manual nodeBinDir change, and the remedy is in the message the warning
# already prints: run both Install/update menu actions.
# Also SHARED across NodeResolver instances for the identical reason: every agent's
# resolver is handed the same project_dir (see LaunchAgent.__init__), so they all read
# and write the one stamp file — the extraction does not make this per-resolver.
INSTALL_NODE_STAMP = ".indigo-node"


def expand_home(path: str, home: str) -> str:
    """Expand a leading ``~`` against the given home dir (no os.environ lookup)."""
    if path.startswith("~"):
        return home + path[1:]
    return path


def _node_major(version: Optional[str]) -> Optional[int]:
    """Major version int from a node version string (``v22.18.0`` → 22), else None."""
    parsed = _parse_node_version(version) if version else None
    return parsed[0] if parsed else None


def _parse_node_version(name: str) -> Optional[tuple[int, ...]]:
    """Parse an nvm node dir / alias label into a comparable version tuple.

    Accepts ``v22.18.0``, ``22.18.0``, ``v22``, ``22`` → ``(22, 18, 0)`` /
    ``(22,)``. Returns ``None`` for non-numeric labels (e.g. ``lts/*``,
    ``default``) which cannot be matched to a version directory directly.
    """
    cleaned = name.strip()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    parts = cleaned.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


class NodeResolver:
    """Resolves and probes the Node/npm toolchain one :class:`~launch_agent.LaunchAgent` runs on.

    :param spec: the owning agent's identity (``package`` and
        ``install_menu_name`` are used in messages).
    :param prefs: the plugin prefs dict — the only pref read here is
        ``nodeBinDir``; everything else about an agent comes from its spec.
    :param home: the resolved home dir. Shared with the owning
        :class:`~launch_agent.LaunchAgent`, not recomputed.
    :param project_dir: the shared npm install root (``~/indigo-matter``).
        Shared with the owning ``LaunchAgent`` and, critically, the SAME value
        across both agents' resolvers — that is what keeps the
        ``.indigo-node`` install stamp shared (see :data:`INSTALL_NODE_STAMP`).
    :param logger: the owning agent's logger.
    :param npx_path: test-injection override. When given, resolution is
        skipped entirely (no filesystem probing, no ``node --version`` calls).
    :param runner: the subprocess seam, shared with the owning ``LaunchAgent``.
    """

    def __init__(
        self,
        spec: Any,
        prefs: dict,
        *,
        home: str,
        project_dir: str,
        logger: Any,
        npx_path: Optional[str] = None,
        runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
    ) -> None:
        self.spec = spec
        self.home = home
        self.project_dir = project_dir
        self.logger = logger
        self._run = runner
        # Optional explicit override: directory containing node/npx. nvm users can
        # pin a specific version here (e.g. ~/.nvm/versions/node/v22.18.0/bin);
        # blank means auto-detect (Homebrew → nvm → PATH). The ONLY pref read here:
        # everything else about an agent comes from its spec.
        raw_bin_dir = str(prefs.get("nodeBinDir", "") or "").strip()
        self.node_bin_dir = expand_home(raw_bin_dir, self.home) if raw_bin_dir else ""
        self.npx_path = npx_path or self._resolve_npx()
        # The node interpreter lives in the same bin dir as npx (Homebrew + nvm both
        # ship node and npx side-by-side). We launch node directly because the
        # matter-server npm package exposes no bin executable (see server_process).
        self.node_path = os.path.join(os.path.dirname(self.npx_path), "node")

    def _resolve_npx(self) -> str:
        """Locate the ``npx`` binary, honouring a USABLE explicit pref then auto-detect.

        Resolution order:
          a. ``nodeBinDir`` pref (``{nodeBinDir}/npx``) — explicit override / pin,
             honoured only if the node beside it actually runs and is new enough
             (see :meth:`_pin_problem`); otherwise auto-detect, loudly.
          b. ``/opt/homebrew/bin/npx`` (Apple-Silicon Homebrew).
          c. ``/usr/local/bin/npx`` (Intel Homebrew).
          d. nvm auto-detect (``~/.nvm/versions/node/<version>/bin/npx``) —
             prefers ``~/.nvm/alias/default``, else highest installed version.
          e. ``shutil.which("npx")`` (whatever's on PATH).
          f. Apple-Silicon Homebrew default as a last resort; ``ensure_installed``
             will log if it's absent. We WARN here so a misconfigured user gets a
             hint rather than a silent dead LaunchAgent.

        **Why (a) is validated at all** (issue #101). The pin used to be trusted
        on an npx-EXISTS check alone, and the install menu re-writes it from
        ``resolved_bin_dir`` after every successful install (the write is in
        ``MatterServerMenuMixin._install_matter_server``, not in ``install()`` itself)
        — so once a pin existed it was self-perpetuating. A user whose pin pointed at a leftover
        ``/usr/local/bin/node`` (an old Node.js ``.pkg``, or Intel Homebrew on an
        Apple-Silicon Mac) got the obvious remedy — install a current node, retry
        — silently shadowed by the pin, and the only symptom was the WS client
        reporting connection refused. The pref is behind "Show advanced server
        settings", so most users do not know the pin exists, let alone that
        clearing it would re-enable auto-detect. Validating it here makes the
        remedy work without any pref surgery.

        Nothing is written to prefs from here: a fallback simply resolves
        elsewhere, and ``_install_matter_server``'s existing post-install pin
        write re-pins to whatever that was on the next CONTROLLER install. (Only
        that one path writes ``nodeBinDir`` — the bridge node's install does not
        — which is why the rejection warning below does not tell the user to run
        "the install menu" and expect the pin to change; this class is shared by
        both agents.) A pin whose fallback ALSO fails survives untouched on disk
        and is used anyway, since a path with no node at all is not an
        improvement on it.

        Note: nvm's version dir is version-specific and changes when the user
        upgrades node. ``ensure_installed()`` re-resolves on every plugin startup,
        so a node upgrade is picked up on the next plugin restart. Set ``nodeBinDir``
        to pin a specific version explicitly.
        """
        # a. explicit override
        if self.node_bin_dir:
            candidate = os.path.join(self.node_bin_dir, "npx")
            if not os.path.exists(candidate):
                self.logger.warning(
                    "nodeBinDir is set to %s but no npx found there; falling back to "
                    "auto-detect", self.node_bin_dir,
                )
            else:
                problem = self._pin_problem(candidate)
                if problem is None:
                    return candidate
                # Resolved BEFORE the warning so the message can name what the
                # pin was shadowing — "falling back" without saying to what
                # leaves the user exactly as stuck as the silent pin did.
                fallback = self._autodetect_npx(exclude_dir=self.node_bin_dir)
                if fallback is None:
                    # Nothing better exists. Using the pin anyway beats resolving
                    # to a path with no node at all: the pin is at least what the
                    # user (or the last install) chose, and the failure it causes
                    # is reported downstream by ensure_installed/install with its
                    # own remedy. Saying so is the point — a warning that claimed
                    # a fallback which did not happen would be worse than silence.
                    self.logger.warning(
                        "The pinned 'Node bin directory' %s is unusable — %s — and "
                        "auto-detect found no other node to use instead. Continuing "
                        "with the pin; install a current Node (e.g. 'brew install "
                        "node'), or point 'Node bin directory' at one under 'Show "
                        "advanced server settings' (blank = auto-detect).",
                        self.node_bin_dir, problem,
                    )
                    return candidate
                self.logger.warning(
                    "IGNORING the pinned 'Node bin directory' %s — %s. Falling back to "
                    "auto-detect, which resolved %s. Nothing was changed on disk: to pin "
                    "a different node yourself, or to clear the pin and keep auto-detect, "
                    "set 'Node bin directory' under 'Show advanced server settings' "
                    "(blank = auto-detect).",
                    self.node_bin_dir, problem, fallback,
                )
                return fallback
        found = self._autodetect_npx()
        if found:
            return found
        # f. last resort
        self.logger.warning(
            "Could not locate npx (checked nodeBinDir, Homebrew, nvm, and PATH). "
            "Set the 'Node bin directory' plugin pref to the folder containing "
            "node/npx (e.g. a ~/.nvm/versions/node/<version>/bin path). Falling "
            "back to %s.", NPX_CANDIDATES[0],
        )
        return NPX_CANDIDATES[0]

    def _pin_problem(self, npx_path: str) -> Optional[str]:
        """Why the pinned bin dir must not be used, or ``None`` if it is fine.

        Two disqualifiers, both about the ``node`` beside the pinned ``npx``
        (which is the binary the LaunchAgent actually execs — the npm package
        exposes no bin entry point):

        * it yields no version — deleted, not executable, an architecture this
          Mac cannot execute, exited non-zero, printed nothing, or hung long
          enough to trip the probe timeout;
        * it runs and reports a version below :data:`MIN_NODE_VERSION`.

        The reason string carries the underlying diagnostic (errno text, or the
        first line of stderr) rather than just "could not be run". "Bad CPU type
        in executable" and "Library not loaded: …libicui18n…" are the answer to
        WHY, and this warning is the one line a user pastes into a support
        thread — issue #101 exists because the only symptom was a connection
        refused that pointed at nothing.

        A node that runs but whose ``--version`` output does not parse is NOT a
        problem: that is an unknown, and ``install()`` already refuses to block
        on an unreadable version for the same reason — refusing a working node
        over a string we failed to read would be the worse failure. Node only
        prints something unparseable for pre-release builds (``v25.0.0-nightly``
        and friends), which are NEWER than the minimum, so rejecting them would
        punish exactly the users most deliberate about their node. Logged at
        debug so the choice is at least auditable.
        """
        node = os.path.join(os.path.dirname(npx_path), "node")
        version, detail = self._probe_node_version_detail(node)
        if version is None:
            return f"the node beside it ({node}) could not be run: {detail}"
        parsed = _parse_node_version(version)
        if parsed is None:
            self.logger.debug(
                "pinned node %s reports %r, which does not parse as a version — accepting it",
                node, version,
            )
            return None
        if parsed[:2] < MIN_NODE_VERSION:
            return (f"its node ({node}) is {version}, older than the required "
                    f"{'.'.join(map(str, MIN_NODE_VERSION))}")
        return None

    def _autodetect_npx(self, *, exclude_dir: str = "") -> Optional[str]:
        """Steps (b)–(e) of :meth:`_resolve_npx` — everything but the pin.

        Split out so a rejected pin can fall back WITHOUT re-entering (a).

        ``exclude_dir`` skips every candidate living in that directory. Splitting
        the method out is NOT on its own enough to stop a rejected pin resolving
        back to itself: ``install()`` pins ``resolved_bin_dir``, which is very
        often ``/opt/homebrew/bin`` or ``/usr/local/bin`` — i.e. steps (b) and
        (c) themselves. Without this, rejecting such a pin walked straight into
        the same directory one step later and logged a warning that contradicted
        itself ("IGNORING …, which resolved <the same path>"), while a healthy
        node further down the chain (nvm, PATH) was never reached.

        Returns None when nothing was found, so the caller can tell "found a
        different node" from "there is no alternative" — those need different
        messages and different outcomes.
        """
        skip = os.path.normpath(exclude_dir) if exclude_dir else ""

        def usable(path: Optional[str]) -> Optional[str]:
            if not path:
                return None
            return None if skip and os.path.normpath(os.path.dirname(path)) == skip else path

        # b + c. Homebrew
        for candidate in NPX_CANDIDATES:
            if os.path.exists(candidate) and usable(candidate):
                return candidate
        # d. nvm
        nvm_npx = usable(self._resolve_nvm_npx())
        if nvm_npx:
            return nvm_npx
        # e. PATH
        return usable(shutil.which("npx"))

    def _resolve_nvm_npx(self) -> Optional[str]:
        """Find an nvm-installed ``npx``.

        Prefers the version named in ``~/.nvm/alias/default`` (a label like
        ``v22``, ``22``, ``v22.18.0`` or ``lts/*``); a partial label like ``22``
        matches the highest installed ``v22.*``. Falls back to the highest
        installed version directory overall. Returns the ``bin/npx`` path if it
        exists, else ``None``. The chosen ``bin`` dir holds BOTH node and npx, so
        the plist PATH (``dirname(npx)``) lets launchd run npx→node.
        """
        versions_dir = os.path.join(self.home, ".nvm", "versions", "node")
        if not os.path.isdir(versions_dir):
            return None
        try:
            installed = [d for d in os.listdir(versions_dir)
                         if os.path.isdir(os.path.join(versions_dir, d))]
        except OSError:
            return None
        if not installed:
            return None

        chosen: Optional[str] = None

        # Prefer the default alias if it resolves to an installed version.
        alias_file = os.path.join(self.home, ".nvm", "alias", "default")
        try:
            with open(alias_file, "r", encoding="utf-8") as handle:
                alias = handle.read().strip()
        except OSError:
            alias = ""
        if alias:
            chosen = self._match_nvm_version(alias, installed)

        # Otherwise (or if the alias didn't resolve) take the highest installed.
        if chosen is None:
            chosen = max(installed, key=lambda d: (_parse_node_version(d) or (-1,)))

        npx = os.path.join(versions_dir, chosen, "bin", "npx")
        return npx if os.path.exists(npx) else None

    @staticmethod
    def _match_nvm_version(alias: str, installed: list[str]) -> Optional[str]:
        """Resolve an nvm alias label to one of the installed version dirs.

        Exact match wins; a partial numeric label (``22`` → ``v22.*``) picks the
        highest matching version. Non-numeric labels (``lts/*``) return ``None``.
        """
        if alias in installed:
            return alias
        if ("v" + alias) in installed:
            return "v" + alias
        wanted = _parse_node_version(alias)
        if wanted is None:
            return None
        matches = [d for d in installed
                   if (_parse_node_version(d) or ())[:len(wanted)] == wanted]
        if not matches:
            return None
        return max(matches, key=lambda d: _parse_node_version(d) or (-1,))

    def abi_warning(self) -> Optional[str]:
        """Return an ADVISORY warning if node's major differs from the install stamp.

        A mismatch *may* mean the package's native bindings won't load — but the stamp
        is only written by this plugin's install(), so a user who reinstalls
        out-of-band (Terminal npm) leaves a STALE stamp behind. Blocking on it would
        refuse a perfectly good server, so this is a warning only: let the server try,
        and if it really is a mismatch it crash-loops and the agent's error log
        surfaces the cause. Never fires when either version is unknown.
        """
        stamped = self._read_install_node_major()
        if stamped is None:
            return None
        current = _node_major(self._node_version())
        if current is None or stamped == current:
            return None
        return (
            f"{self.spec.package} was installed with Node {stamped}.x but the resolved node "
            f"({self.node_path}) is {current}.x. If it fails to start, reinstall via "
            f"Plugins ▸ Matter ▸ {self.spec.install_menu_name}, or clear the stale stamp "
            f"({self._install_stamp_path()})."
        )

    def _node_version(self) -> Optional[str]:
        """Return the resolved node's version string (e.g. ``v22.18.0``), or None."""
        return self._probe_node_version(self.node_path)

    def _probe_node_version(self, node_path: str) -> Optional[str]:
        """``{node_path} --version`` → e.g. ``v22.18.0``, or None if it did not run."""
        return self._probe_node_version_detail(node_path)[0]

    def _probe_node_version_detail(self, node_path: str) -> tuple[Optional[str], str]:
        """As :meth:`_probe_node_version`, plus WHY it failed.

        Returns ``(version, detail)``. ``version`` is None when the probe did
        not yield one, and ``detail`` then says which of the four ways it went
        wrong — those are genuinely different faults and only one of them is
        "did not run", so collapsing them into a single message throws away the
        one string that would tell a user what to do.

        Takes the path rather than reading :attr:`node_path` because
        :meth:`_pin_problem` runs during ``__init__``, BEFORE ``node_path``
        exists — it is deciding which bin dir that attribute will be derived
        from.

        ``timeout`` is not optional here. This runs on the plugin startup path
        (``__init__`` → :meth:`_resolve_npx`), and a node binary on a stale
        network mount or a sleeping external disk can block on exec
        indefinitely — which would hang Indigo's plugin startup with nothing in
        the log, since the warning is only written after the probe returns.
        ``TimeoutExpired`` is a ``SubprocessError``, NOT an ``OSError``, so it
        has to be caught explicitly alongside it.
        """
        try:
            result = self._run([node_path, "--version"], capture_output=True,
                               text=True, check=False, timeout=NODE_PROBE_TIMEOUT)
        except OSError as exc:
            return None, str(exc)
        except subprocess.SubprocessError as exc:
            return None, f"it did not respond within {NODE_PROBE_TIMEOUT}s ({exc.__class__.__name__})"
        if result is None:
            return None, "no result from the probe"
        if result.returncode != 0:
            stderr = (getattr(result, "stderr", "") or "").strip().splitlines()
            first = stderr[0] if stderr else "no error output"
            return None, f"it exited {result.returncode}: {first}"
        version = (result.stdout or "").strip()
        if not version:
            return None, "it ran but reported no version"
        return version, ""

    def _install_stamp_path(self) -> str:
        return os.path.join(self.project_dir, INSTALL_NODE_STAMP)

    def _read_install_node_major(self) -> Optional[int]:
        try:
            with open(self._install_stamp_path(), "r", encoding="utf-8") as handle:
                return _node_major(handle.read().strip())
        except OSError:
            return None

    def _record_install_node(self) -> None:
        version = self._node_version()
        if not version:
            return
        try:
            with open(self._install_stamp_path(), "w", encoding="utf-8") as handle:
                handle.write(version + "\n")
        except OSError as exc:  # pragma: no cover - best-effort stamp
            self.logger.warning("could not record install node version: %s", exc)
