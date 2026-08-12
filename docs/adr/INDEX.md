# Architecture Decision Records — indigo-matter

Repo-local decisions. **Cross-repo concerns live in the workspace index instead**
(`../../../docs/adr/INDEX.md`) — the push contract, shared auth, the HMAC scheme,
event schemas, and the confinement of matter.js to the bridge node.

**Numbering is independent of the workspace's and collides with it** — both
sequences start at 0001. Always qualify a reference as "workspace ADR-NNNN" when
pointing outside this directory.

Read this before changing writable settings, capability detection, or the
diagnostics surface. **ADRs are immutable once accepted** — supersede with a new
one, never edit. For how the current design *works* (rather than why it was
chosen), see [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

<!-- adrlog -->

* [ADR-0000](0000-template.md) - ADR-NNNN: {short title, imperative}
* [ADR-0001](0001-record-architecture-decisions.md) - ADR-0001: Record repo-local architecture decisions here (accepted)
* [ADR-0002](0002-writable-settings-are-a-declarative-registry.md) - ADR-0002: Writable device settings are a declarative registry, edited in the Edit Device dialog (accepted)
* [ADR-0003](0003-attributelist-is-the-capability-authority.md) - ADR-0003: A device's own AttributeList is the only capability evidence (accepted)
* [ADR-0004](0004-matter-diagnostics-are-read-only.md) - ADR-0004: The Matter attribute diagnostics are read-only, permanently (accepted)
* [ADR-0005](0005-command-parameters-are-not-settings.md) - ADR-0005: A command parameter is not a setting (accepted; narrows what ADR-0002 may declare; **superseded by ADR-0006**)
* [ADR-0006](0006-a-curated-exclusion-list-cross-checked-by-the-generator.md) - ADR-0006: A curated exclusion list, cross-checked by the generator, decides what is not a setting (accepted; supersedes ADR-0005)
* [ADR-0007](0007-a-retired-everywhere-setting-keeps-its-state-flagged.md) - ADR-0007: A setting retired everywhere keeps its state, flagged — only a missing capability withdraws it (accepted; narrows ADR-0003)
* [ADR-0008](0008-a-matter-node-is-an-indigo-device.md) - ADR-0008: A Matter node is an Indigo device, and the root of its endpoint devices' group (accepted)

<!-- adrlogstop -->
