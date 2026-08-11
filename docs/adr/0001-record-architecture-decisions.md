---
parent: Decisions
nav_order: 1
title: "ADR-0001: Record repo-local architecture decisions here"

status: "accepted"
date: 2026-08-11
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0001: Record repo-local architecture decisions here

## Context and Problem Statement

The workspace has carried ADRs since ADR-0001 at the root, and the workspace
`CLAUDE.md` says each inner repo "may also carry its own `docs/adr/` for
repo-local decisions". This repo never created one.

The consequence was not that decisions went unmade — it is that they were
recorded in places nobody else can read. Several load-bearing calls (how writable
settings are declared, what counts as evidence that a device implements an
attribute, whether the diagnostics may ever write) existed only in the
maintainer's private assistant memory, in issue comments, and in code docstrings.
This plugin is public and has had at least one external contributor (#73), so a
decision recorded privately is a decision that will be re-litigated by the next
person — or silently violated.

## Decision Drivers

* Decisions with real reasoning behind them should outlive the session that made
  them, and be visible to anyone reading the repo.
* The workspace already has a format, a template and a convention — forking it
  would be worse than not having one.
* Not every decision here is cross-repo. Confining matter.js to the bridge node
  affects the whole workspace; how `DeviceSetting` bounds are declared does not.
* `docs/ARCHITECTURE.md` already explains *how* each module works. Duplicating
  that into ADRs would guarantee the two drift.

## Considered Options

* Keep using workspace `docs/adr/` for everything
* Create a repo-local `docs/adr/`
* Record decisions in `docs/ARCHITECTURE.md` alongside the module descriptions

## Decision Outcome

Chosen option: "Create a repo-local `docs/adr/`", because plugin-internal
decisions do not belong in a workspace index that exists to answer cross-repo
questions, and because `ARCHITECTURE.md` answers "how does this work?" whereas an
ADR answers "why this way, what else did we consider, and what must not be
revisited?" — different questions with different lifetimes.

**The boundary rule:**

* **Workspace `docs/adr/`** — anything spanning two or more repos: the push
  contract, shared auth, the HMAC scheme, event schemas, the Domio↔plugin API,
  and the confinement of matter.js to the bridge node (workspace ADR-0006).
* **This directory** — decisions internal to the plugin: how settings are
  declared, what evidence proves a device capability, what the diagnostics
  surface may and may not do.

**Numbering is independent and collides with the workspace's.** Both sequences
start at 0001. Every reference must therefore be qualified: write "workspace
ADR-0006", never a bare "ADR-0006", when pointing outside this directory.

Format is MADR 4.0.0 using `0000-template.md`, copied byte-identical from the
workspace root. The template's non-standard `## For AI agents` section is not
optional here: it carries the operative DO/DON'T rules, so an agent that opens
one ADR gets the rule without having to read the whole argument.

### Consequences

* Good, because the reasoning is visible to contributors and survives the
  maintainer forgetting it.
* Good, because "do not re-pitch this" becomes checkable rather than a matter of
  whose memory is loaded.
* Bad, because a repo doc is not loaded automatically the way assistant memory
  is — so an index pointer has to be maintained separately, or the ADR is
  invisible in practice. Mitigated by `CLAUDE.md` naming this directory under
  Standards, and by memory keeping a one-line pointer per decision.
* Bad, because two independent numbering sequences invite ambiguous references.
  Mitigated only by the qualification rule above, which is easy to forget.

### Confirmation

`INDEX.md` lists every file in this directory and nothing else; `CLAUDE.md`
points here. A decision is "recorded" when a reader who has never seen the code
can say what was rejected and why.

## Pros and Cons of the Options

### Keep using workspace `docs/adr/`

* Good, because one sequence, one index, no ambiguity in references.
* Bad, because the workspace index exists to be read "before any architectural
  task spanning two or more repos" — filling it with plugin internals makes that
  instruction expensive to follow.
* Bad, because it buries genuinely cross-cutting decisions among local ones.

### Create a repo-local `docs/adr/`

* Good, because the decision sits next to the code it governs.
* Good, because four sibling repos already do this (`domio-code`,
  `indigo-home-intelligence`, `indigo-mcp-lite`, `indigo-domio-plugin`), so the
  pattern is established rather than invented here.
* Bad, because numbering now collides across repos.

### Record decisions in `docs/ARCHITECTURE.md`

* Good, because it is already the one place a contributor is told to read.
* Bad, because that file describes the *current* design; an ADR must record
  rejected alternatives and stay immutable once accepted. Mixing the two means
  either the history pollutes the description or the history gets edited away.

## More Information

Workspace rules: root `CLAUDE.md` § Architecture Decision Records. Workspace
index: `../../../docs/adr/INDEX.md`. Module-level description of the current
design: [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

**ADRs are immutable once accepted** — supersede with a new one, never edit.

## For AI agents
- DO: read `INDEX.md` here before changing writable settings, capability
  detection, or the diagnostics surface.
- DO: qualify every cross-directory reference as "workspace ADR-NNNN".
- DO: propose a new ADR here when making a plugin-internal decision that a future
  reader could reasonably reverse without knowing what it cost.
- DON'T: edit an accepted ADR to reflect a new decision — supersede it.
- DON'T: restate `ARCHITECTURE.md`'s module descriptions in an ADR; link instead.
