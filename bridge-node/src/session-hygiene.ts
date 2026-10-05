/**
 * App-level CASE session hygiene (issue #283 "Finding 2").
 *
 * The banked recovery playbook for the recurring Alexa subscription-staleness
 * symptom: a controller (Alexa most often, per issue #400 on
 * riddix/home-assistant-matter-hub) piles up CASE sessions for one peer
 * without ever closing the old ones, and matter.js 0.17.8's own
 * subscription-update routing picks the peer's most-recently-*active*
 * session rather than the one a subscription was actually created on
 * (measured, §0(o); the defect this module works AROUND, not the one it
 * fixes — that fix belongs upstream in matter.js, see the issue). A report
 * can land on a session the controller isn't reading from: it MRP-acks the
 * frame and discards it. This module removes the pile-up precondition —
 * closing superseded sessions before they can pile — entirely through
 * matter.js's PUBLIC session-layer API (§0(k)/(l)); no dependency patch, per
 * the issue's explicit scope.
 *
 * **What is built: the superseded-session sweep** ({@link supersededSessions})
 * — when a peer opens a new CASE session, its older ones are closed
 * immediately. It targets the pile-up precondition directly, at a cap of ONE
 * session per peer rather than matter.js's own built-in cap of five (§0(k)),
 * which the reference-server recurrence proved too high — the routing defect
 * bit at three piled sessions, not six.
 *
 * **Deliberately closes a session even while it still holds a live
 * subscription.** Orphaning the OLD session's subscription is the point, not
 * a side effect to tolerate: the peer that superseded it has, by definition,
 * a NEW session already open, and the HAMH-proven behaviour is that it
 * re-subscribes over that one.
 *
 * **Why the superseded sweep is the only close — the dead/rotated closes were REMOVED.**
 * Earlier versions also force-closed (a) "dead" sessions — zero
 * subscriptions, quiet for 60 s — and (b) "rotated" ones — zero
 * subscriptions, older than 4 h. Both were a bug. In matter.js 0.17.8
 * `NodeSession.initiateForceClose` sets `#isPeerLost`, so `close()` skips the
 * graceful-close emit and **no CloseSession is sent: the peer is never told
 * its session is gone**. A controller whose session holds no subscription
 * (Alexa's command/read session) was therefore treated as dead 60 s after its
 * last message and had it silently dropped, then sent its next voice command
 * into the void. The bridge logged "Ignoring message for unknown session";
 * the Echo retransmitted for ~15 s before falling back to CASE resume (235
 * such stalls measured, median 15.3 s). The superseded sweep is safe by
 * contrast because the peer itself opened the replacement session; it always
 * has somewhere to go. A session no peer has replaced is never ours to take
 * away behind the controller's back.
 *
 * The #283 pile-up protection this leaves in place: the superseded sweep (cap
 * of one session per peer+fabric), plus matter.js's own per-peer cap and LRU
 * eviction for anything the sweep cannot see. Caveat: that LRU eviction
 * (`SessionManager.js`, cap 5) also uses `initiateForceClose`, so the backstop
 * is not free of the same silent drop; with the sweep holding one session per
 * peer+fabric it should not trigger.
 *
 * **Considered and not built: a graceful close.** Sending a real
 * CloseSession before dropping a quiet session was weighed and rejected —
 * how a given controller reacts to one cannot be verified from here, and it
 * would still end a session a polling controller regards as perfectly
 * healthy. Raising the quiet threshold instead of removing the close would
 * only have reduced how often the stall occurs, not removed it.
 *
 * **The wedge watchdog ("controller ACKs but stops issuing new IM
 * requests") is deliberately NOT built.** §0(m) traced the one signal that
 * could distinguish "acked" from "consumed" —
 * `MessageExchange.#messageReceivedCounter` — and found it private, with no
 * getter, and scoped to one exchange rather than accumulated per session.
 * The public-facing `Session.activeTimestamp`/`isPeerActive` advance on a
 * bare MRP acknowledgement exactly as on a genuine `SubscribeRequest`
 * (`MessageExchange.onMessageReceived` calls `#notifyActivity(true)`
 * *before* it branches on `isStandaloneAck`), so there is no public vantage
 * point from which this module could ever tell the two apart. Building it
 * would mean patching matter.js internals to expose the counter, which is
 * exactly the "no dependency patching" line issue #283 draws for Finding 1
 * — the same line applies here. If this is ever revisited, the internal
 * that would need to become public is
 * `@matter/protocol/protocol/MessageExchange.ts`'s `#messageReceivedCounter`
 * (or an equivalent per-session, ack-excluded activity counter).
 *
 * **Deliberately pure — no matter.js import, no timer, injected clock —**
 * for the same reason `churn.ts` is (its file header explains why; the short
 * version is that the interesting states need a real controller and real
 * time to reach, so a test needs to be able to fabricate both). `node.ts`
 * owns the thin wiring: it hooks `SessionManager`'s `sessions.added` for the
 * superseded sweep (immediate, event-driven — a pile-up precondition that
 * waited for the next `get_status` poll would already have let a report
 * misroute) and calls {@link peerSessionCounts} from `getStatus()` for the
 * read-only per-peer diagnostic.
 *
 * Every function here is stateless: given a snapshot of session descriptors,
 * decide what to close and why. `node.ts` is what actually calls
 * `session.initiateForceClose(...)`, logs once per action, and keeps the
 * cumulative counts §4.3 reports (`closed.dead`/`closed.rotated` stay on the
 * wire for shape stability and are now permanently 0).
 */

import type { SessionHygienePeer } from "./protocol.js";

/** Why {@link supersededSessions} decided to close a session (the only remaining reason). */
export type HygieneReason = "superseded";

/**
 * A `NodeSession`, reduced to the plain fields this module works with.
 * The superseded sweep decides from `createdAt`, the peer id and the fabric
 * index only; `activeTimestamp` and `subscriptionCount` are informational
 * (kept for tests and diagnostics) and no decision reads them.
 * `node.ts` builds these from the real matter.js objects; nothing here
 * imports matter.js so these fields are plain, clock-comparable numbers
 * rather than the branded `Timestamp` type matter.js declares them as
 * (assignable directly — a `Timestamp` is a `number` at the value level).
 */
export interface SessionDescriptor {
    sessionId: number;
    /** Hex, as matter.js logs it — matches `churn.ts`'s `peerNodeIdHex`. */
    peerNodeId: string;
    fabricIndex: number;
    /** `Session.createdAt` — ms since epoch, stamped once at construction. */
    createdAt: number;
    /** `Session.activeTimestamp` — ms since epoch, last message RECEIVED. Informational only; no decision reads it. */
    activeTimestamp: number;
    /** `session.subscriptions.size` at the moment of the check. Informational only; no decision reads it. */
    subscriptionCount: number;
}

/** One session {@link supersededSessions} decided to close. */
export interface HygieneClosure {
    sessionId: number;
    peerNodeId: string;
    fabricIndex: number;
    reason: HygieneReason;
    /** The session's age at the moment of closure. */
    ageMs: number;
    /** How many sessions the peer held, including the new one. */
    peerSessionCount: number;
}

/**
 * The core deliverable (issue #283 Finding 2): a peer that just
 * opened a new CASE session gets its OLDER ones closed immediately.
 *
 * `peerSessions` must already be scoped to one peer (same `peerNodeId` +
 * `fabricIndex`) — `node.ts` does that scoping against the live
 * `SessionManager` set, the same filter `SessionManager`'s own
 * `#evictExcessSessionsFor` uses on itself (§0(k)), just capped at 1 instead
 * of `MAX_SESSIONS_PER_PEER` (5). It must include the just-opened session
 * (identified by `justOpenedSessionId`) — everything else in the array is a
 * candidate to close.
 *
 * Returns `[]` (rather than throwing) when `justOpenedSessionId` is not
 * actually present in `peerSessions` — a caller-side inconsistency (the
 * event fired for a session that has since closed) must degrade to "nothing
 * to sweep", not crash the caller's event handler.
 *
 * **Every OTHER session for the peer is a candidate, `subscriptionCount`
 * included** — this function never checks it. That is the deliberate bet the
 * module docstring explains: `justOpened` IS the replacement the controller
 * will re-subscribe over, so orphaning an older session's subscription here
 * is the intended outcome. (Sessions with NO replacement in hand are never
 * closed by this module at all.)
 */
export function supersededSessions(
    peerSessions: readonly SessionDescriptor[],
    justOpenedSessionId: number,
): HygieneClosure[] {
    const justOpened = peerSessions.find(session => session.sessionId === justOpenedSessionId);
    if (justOpened === undefined) {
        return [];
    }
    return peerSessions
        .filter(session => session.sessionId !== justOpenedSessionId)
        .map(session => ({
            sessionId: session.sessionId,
            peerNodeId: session.peerNodeId,
            fabricIndex: session.fabricIndex,
            reason: "superseded" as const,
            ageMs: Math.max(0, justOpened.createdAt - session.createdAt),
            peerSessionCount: peerSessions.length,
        }));
}

/**
 * Issue #283's own "diagnostic to run first" (the issue body's recipe: count
 * live CASE sessions per Echo peer) — the per-peer counts for §4.3.
 *
 * Deliberately every peer holding at least one live CASE session, NOT only
 * ones over a threshold: this is a standing diagnostic a human reads to spot
 * a pile *forming*, and `churn.ts`'s `SubscriptionChurn.peers` (over-threshold
 * only) already covers the "act now" case. Sorted by peer id for a stable
 * diff between polls, matching `ChurnDetector.verdict`'s own ordering.
 */
export function peerSessionCounts(sessions: readonly SessionDescriptor[]): SessionHygienePeer[] {
    const counts = new Map<string, SessionHygienePeer>();
    for (const session of sessions) {
        const key = `${session.fabricIndex}/${session.peerNodeId}`;
        const existing = counts.get(key);
        if (existing !== undefined) {
            existing.liveSessions++;
        } else {
            counts.set(key, { peerNodeId: session.peerNodeId, fabricIndex: session.fabricIndex, liveSessions: 1 });
        }
    }
    return [...counts.values()].sort((a, b) => a.peerNodeId.localeCompare(b.peerNodeId));
}
