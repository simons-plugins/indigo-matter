/**
 * The loopback interface to pin matter.js's mDNS responder to, shared by every
 * test that stands up a real `ServerNode`.
 *
 * ⊗ #330. Without this, matter.js broadcasts mDNS on **every** interface the
 * host has, VPN tunnels included, and a send queued against a tunnel that is
 * not carrying traffic never completes: the process is left holding UDP 5353
 * sockets and pending `SendWrap` requests, so node's test runner cannot exit.
 * `--test-force-exit` was papering over that — and paid for it by truncating a
 * concurrently-forked file mid-report, which is how a green run came to
 * silently skip whole suites.
 *
 * Pinning also keeps a test run off the real network: nothing here has any
 * business advertising a Matter bridge on somebody's actual LAN.
 *
 * `undefined` if the host somehow has no internal interface, in which case the
 * option is simply omitted and behaviour is what it was before.
 */
import { networkInterfaces } from "node:os";

import { Environment } from "@matter/main";
import { MdnsService } from "@matter/protocol";

export const LOOPBACK: string | undefined = Object.entries(networkInterfaces()).find(
    ([, addresses]) => addresses?.some(address => address.internal),
)?.[0];

/**
 * Close the mDNS responder shared by every node in this process.
 *
 * `ServerNode.close()` does not: `MdnsService` is a service on the *Environment*
 * — `Environment.default`, a process-wide singleton — not on any one node, so it
 * outlives the last node that used it and keeps its four UDP sockets (broadcaster
 * and scanner, v4 and v6) open. That is enough to stop node's test runner
 * exiting even when every node has been closed, which is the second half of
 * #330. Call it from an `after()` in any file that starts a real `ServerNode`.
 *
 * Safe to call when no responder was ever created, and safe to call twice.
 */
export async function closeSharedMdns(): Promise<void> {
    const environment = Environment.default;
    if (!environment.has(MdnsService)) {
        return;
    }
    await environment.get(MdnsService).close();
}

/**
 * Give matter.js's fire-and-forget storage writes a chance to land before a
 * test file deletes its scratch directories.
 *
 * ⊗ #330, second half. `VolatileEventStore.add()` issues
 * `storage.set("lastEventNumber", …)` and never awaits it
 * (`@matter/protocol`, VolatileEventStore.js) — so a write can still be in
 * flight after `ServerNode.close()` has resolved. `rmSync` the directory in
 * that window and the write fails `ENOENT`, which surfaces as an
 * `unhandledRejection` that node's test runner attributes to whichever test
 * happened to be last. `--test-force-exit` hid it by killing the process first.
 *
 * A macrotask turn is enough for the already-queued `fs` operation. This does
 * not make the race impossible — only matter.js awaiting its own write could —
 * so cleanup must stay tolerant (`rmSync(..., { force: true })`).
 */
export async function settleMatterWrites(): Promise<void> {
    await new Promise(resolve => setTimeout(resolve, 50));
}
