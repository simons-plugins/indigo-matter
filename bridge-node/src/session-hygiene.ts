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
 * closing superseded/dead/aged sessions before they can pile — entirely
 * through matter.js's PUBLIC session-layer API (§0(k)/(l)); no dependency
 * patch, per the issue's explicit scope.
 *
 * Three of Finding 2's four mechanisms are built:
 *
 * 1. **Superseded-session sweep** ({@link supersededSessions}) — when a peer
 *    opens a new CASE session, its older ones are closed immediately. The
 *    core deliverable: it targets the pile-up precondition directly, at a
 *    cap of ONE session per peer rather than matter.js's own built-in cap of
 *    five (§0(k)), which the reference-server recurrence proved too high —
 *    the routing defect bit at three piled sessions, not six.
 *
 *    **Deliberately closes a session even while it still holds a live
 *    subscription** — unlike items 2/3 below, which both refuse to (§0(n)).
 *    Orphaning the OLD session's subscription is the point, not a side
 *    effect to tolerate: the peer that superseded it has, by definition, a
 *    NEW session already open, and the HAMH-proven behaviour is that it
 *    re-subscribes over that one. Items 2/3 have no such replacement in
 *    hand for the session they would be closing — closing one of THOSE
 *    under a live subscription would strand it with nothing to recover
 *    onto, which is the staleness pattern this whole module exists to
 *    prevent. Same bet, opposite session, hence the opposite rule.
 * 2. **Dead-session force-close** ({@link deadSessions}) — a CASE session
 *    holding zero subscriptions that has been quiet for
 *    {@link DEAD_SESSION_QUIET_MS} is closed. A session that was never
 *    superseded (the sweep above only fires on a NEW session arriving) but
 *    was simply abandoned — a controller that opened a session, read once,
 *    and never came back — would otherwise sit forever.
 * 3. **Age-based rotation** ({@link rotatableSessions}) — a CASE session
 *    older than {@link SESSION_MAX_AGE_MS} is closed, but **only when it
 *    holds zero subscriptions**. This is narrower than HAMH's own 4h
 *    rotation, deliberately: §0(n) establishes that a
 *    `ServerSubscription` is bound to the session it was created on for
 *    life (`ServerSubscription.session` never reassigns), so closing the
 *    session under a LIVE subscription does not migrate it — it strands it,
 *    manufacturing the exact staleness pattern this module exists to
 *    prevent, and no live controller was available to this probe to verify
 *    otherwise. Rotating only subscription-free sessions is the safe
 *    subset; a subscription-holding session ages out only via the
 *    superseded sweep, when the controller itself opens a replacement.
 *
 * **Item 4, the wedge watchdog ("controller ACKs but stops issuing new IM
 * requests"), is deliberately NOT built.** §0(m) traced the one signal that
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
 * misroute) and calls {@link periodicSweep} from `getStatus()` for the
 * dead/rotated checks, the same poll-driven idiom `churn.ts`'s
 * `verdict()`/`poll()` already established for recurring session-layer
 * checks — not a new `setInterval`, and not dependent on the plugin's
 * ~15s watchdog cadence being exact: both thresholds (60s, 4h) are wide
 * enough that a check running "whenever `get_status` is next called" is a
 * few seconds' slop, not a functional gap.
 *
 * Every function here is stateless: given a snapshot of session descriptors
 * and a clock reading, decide what to close and why. `node.ts` is what
 * actually calls `session.initiateForceClose(...)`, logs once per action,
 * and keeps the cumulative counts §4.3 reports.
 */

import type { SessionHygienePeer } from "./protocol.js";

/** Why {@link periodicSweep}/{@link supersededSessions} decided to close a session. */
export type HygieneReason = "superseded" | "dead" | "rotated";

/**
 * A `NodeSession`, reduced to what this module's pure decisions need.
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
    /** `Session.activeTimestamp` — ms since epoch, last message RECEIVED. */
    activeTimestamp: number;
    /** `session.subscriptions.size` at the moment of the check. */
    subscriptionCount: number;
}

/** One session {@link periodicSweep}/{@link supersededSessions} decided to close. */
export interface HygieneClosure {
    sessionId: number;
    peerNodeId: string;
    fabricIndex: number;
    reason: HygieneReason;
    /** The session's age at the moment of closure. */
    ageMs: number;
    /** `dead` only — how long it had held zero subscriptions. */
    quietMs?: number;
    /** `superseded` only — how many sessions the peer held, including the new one. */
    peerSessionCount?: number;
}

/**
 * How long a CASE session may hold zero subscriptions before
 * {@link deadSessions} closes it.
 *
 * 60s, per issue #283 Finding 2 (HAMH's own #105/#266 uses the same figure
 * for its non-"fast recovery" mode; that faster 5s variant is not requested
 * by #283 and is not built here — a narrower scope than the source
 * playbook, not an oversight). Long enough that an ordinary read-then-idle
 * session (a controller doing a one-off `ReadRequest` with no subscribe)
 * survives comfortably; short enough that an abandoned session does not sit
 * for the full {@link SESSION_MAX_AGE_MS} window before anything notices it.
 */
export const DEAD_SESSION_QUIET_MS = 60_000;

/**
 * How old a subscription-free CASE session may get before
 * {@link rotatableSessions} closes it. 4 hours, per issue #283 Finding 2.
 *
 * Restricted to subscription-free sessions only — see the module docstring
 * and §0(n) for why a session under a live subscription is never rotated by
 * age: matter.js provides no way to migrate that subscription to whatever
 * session the controller opens next, so doing so would strand it rather
 * than rotate it cleanly.
 */
export const SESSION_MAX_AGE_MS = 4 * 60 * 60 * 1000;

/**
 * The core deliverable (issue #283 Finding 2 item 1): a peer that just
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
 * included** — unlike {@link deadSessions}/{@link rotatableSessions}, this
 * function never checks it. That is the deliberate bet the module docstring
 * (item 1) explains: `justOpened` IS the replacement the controller will
 * re-subscribe over, so orphaning an older session's subscription here is
 * the intended outcome, not a hazard to guard against the way it would be
 * for a dead or aged-out session with no such replacement in hand.
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
 * Issue #283 Finding 2 item 2 — a CASE session holding zero subscriptions,
 * quiet for at least `quietMs`. `sessions` may span every peer; each
 * descriptor is judged independently.
 */
export function deadSessions(
    sessions: readonly SessionDescriptor[],
    nowMs: number,
    quietMs: number = DEAD_SESSION_QUIET_MS,
): HygieneClosure[] {
    return sessions
        .filter(session => session.subscriptionCount === 0 && nowMs - session.activeTimestamp >= quietMs)
        .map(session => ({
            sessionId: session.sessionId,
            peerNodeId: session.peerNodeId,
            fabricIndex: session.fabricIndex,
            reason: "dead" as const,
            ageMs: Math.max(0, nowMs - session.createdAt),
            quietMs: Math.max(0, nowMs - session.activeTimestamp),
        }));
}

/**
 * Issue #283 Finding 2 item 3 — a subscription-free CASE session older than
 * `maxAgeMs`. See the module docstring / §0(n) for why a session holding a
 * live subscription is never a candidate here.
 */
export function rotatableSessions(
    sessions: readonly SessionDescriptor[],
    nowMs: number,
    maxAgeMs: number = SESSION_MAX_AGE_MS,
): HygieneClosure[] {
    return sessions
        .filter(session => session.subscriptionCount === 0 && nowMs - session.createdAt >= maxAgeMs)
        .map(session => ({
            sessionId: session.sessionId,
            peerNodeId: session.peerNodeId,
            fabricIndex: session.fabricIndex,
            reason: "rotated" as const,
            ageMs: Math.max(0, nowMs - session.createdAt),
        }));
}

/**
 * {@link deadSessions} then {@link rotatableSessions} on whatever is left,
 * so a session that qualifies for both (subscription-free, quiet AND past
 * the age ceiling) is reported once, as `dead` — the tighter, more specific
 * check — rather than twice or as the less specific `rotated`. This is what
 * `node.ts` calls from `getStatus()`; it never needs to call the two
 * individual functions itself.
 */
export function periodicSweep(
    sessions: readonly SessionDescriptor[],
    nowMs: number,
    options: { deadQuietMs?: number; maxAgeMs?: number } = {},
): HygieneClosure[] {
    const dead = deadSessions(sessions, nowMs, options.deadQuietMs);
    const deadIds = new Set(dead.map(closure => closure.sessionId));
    const remaining = sessions.filter(session => !deadIds.has(session.sessionId));
    return [...dead, ...rotatableSessions(remaining, nowMs, options.maxAgeMs)];
}

/**
 * Issue #283's own "diagnostic to run first" (the issue body's recipe: count
 * live CASE sessions per Echo peer) — item 5's per-peer counts for §4.3.
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
