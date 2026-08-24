/**
 * Issue #283 "Finding 2" — the pure session-hygiene decisions, on plain
 * descriptors and a hand-moved clock. No matter.js here by design
 * (`session-hygiene.ts` imports none) — `persistence.test.ts` pins the
 * `node.ts` wiring (peer-scoping the sweep, closing real sessions) against a
 * real started node, the same split `churn.ts`/`churn.test.ts` established.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
    DEAD_SESSION_QUIET_MS,
    deadSessions,
    peerSessionCounts,
    periodicSweep,
    rotatableSessions,
    SESSION_MAX_AGE_MS,
    type SessionDescriptor,
    supersededSessions,
} from "../src/session-hygiene.js";

const ECHO = "41869fbd537ef01";
const OTHER_ECHO = "9f2c00114b3d201";
const FABRIC = 2;
const NOW = 1_700_000_000_000;

function session(overrides: Partial<SessionDescriptor> & { sessionId: number }): SessionDescriptor {
    return {
        peerNodeId: ECHO,
        fabricIndex: FABRIC,
        createdAt: NOW,
        activeTimestamp: NOW,
        subscriptionCount: 0,
        ...overrides,
    };
}

describe("supersededSessions (issue #283 item 1)", () => {
    it("closes every OTHER session for the peer, none of the just-opened one", () => {
        const peerSessions = [
            session({ sessionId: 1, createdAt: NOW - 10_000 }),
            session({ sessionId: 2, createdAt: NOW - 5_000 }),
            session({ sessionId: 3, createdAt: NOW }), // the just-opened one
        ];
        const closures = supersededSessions(peerSessions, 3);
        assert.deepEqual(closures.map(c => c.sessionId).sort(), [1, 2]);
        for (const closure of closures) {
            assert.equal(closure.reason, "superseded");
            assert.equal(closure.peerSessionCount, 3);
            assert.equal(closure.peerNodeId, ECHO);
            assert.equal(closure.fabricIndex, FABRIC);
        }
    });

    it("reports the closed session's age relative to the just-opened one", () => {
        const peerSessions = [
            session({ sessionId: 1, createdAt: NOW - 30_000 }),
            session({ sessionId: 2, createdAt: NOW }),
        ];
        const [closure] = supersededSessions(peerSessions, 2);
        assert.equal(closure!.ageMs, 30_000);
    });

    it("returns nothing when the just-opened session is not in the array", () => {
        // The caller-side inconsistency case: the event fired for a session
        // that has since closed. Must degrade to "nothing to sweep", not throw.
        const peerSessions = [session({ sessionId: 1 })];
        assert.deepEqual(supersededSessions(peerSessions, 999), []);
    });

    it("closes nothing when the peer holds only the just-opened session", () => {
        assert.deepEqual(supersededSessions([session({ sessionId: 1 })], 1), []);
    });

    it("closes a superseded session even while it holds a live subscription — deliberately, unlike deadSessions/rotatableSessions", () => {
        // The bet: `justOpened` (session 2) IS the replacement the controller
        // will re-subscribe over, so orphaning session 1's subscription here
        // is the intended outcome — not an oversight `subscriptionCount`
        // should have guarded against, the way it does for items 2/3.
        const peerSessions = [
            session({ sessionId: 1, createdAt: NOW - 10_000, subscriptionCount: 1 }),
            session({ sessionId: 2, createdAt: NOW }),
        ];
        const closures = supersededSessions(peerSessions, 2);
        assert.deepEqual(closures.map(c => c.sessionId), [1]);
    });
});

describe("deadSessions (issue #283 item 2)", () => {
    it("closes a subscription-free session quiet for the full window", () => {
        const sessions = [session({ sessionId: 1, activeTimestamp: NOW - DEAD_SESSION_QUIET_MS })];
        const closures = deadSessions(sessions, NOW);
        assert.equal(closures.length, 1);
        assert.equal(closures[0]!.reason, "dead");
        assert.equal(closures[0]!.quietMs, DEAD_SESSION_QUIET_MS);
    });

    it("never closes a session that holds a subscription, however quiet", () => {
        const sessions = [
            session({ sessionId: 1, activeTimestamp: NOW - DEAD_SESSION_QUIET_MS * 10, subscriptionCount: 1 }),
        ];
        assert.deepEqual(deadSessions(sessions, NOW), []);
    });

    it("does not close a subscription-free session inside the quiet window", () => {
        const sessions = [session({ sessionId: 1, activeTimestamp: NOW - (DEAD_SESSION_QUIET_MS - 1) })];
        assert.deepEqual(deadSessions(sessions, NOW), []);
    });

    it("respects a custom quiet threshold", () => {
        const sessions = [session({ sessionId: 1, activeTimestamp: NOW - 5_000 })];
        assert.equal(deadSessions(sessions, NOW, 5_000).length, 1);
        assert.equal(deadSessions(sessions, NOW, 5_001).length, 0);
    });
});

describe("rotatableSessions (issue #283 item 3)", () => {
    it("closes a subscription-free session past the age ceiling", () => {
        const sessions = [session({ sessionId: 1, createdAt: NOW - SESSION_MAX_AGE_MS })];
        const closures = rotatableSessions(sessions, NOW);
        assert.equal(closures.length, 1);
        assert.equal(closures[0]!.reason, "rotated");
        assert.equal(closures[0]!.ageMs, SESSION_MAX_AGE_MS);
    });

    it("never closes a session that holds a subscription, however old — §0(n)", () => {
        // A ServerSubscription's `#context.session` is fixed at construction
        // and never reassigned: force-closing the session it lives on would
        // strand the subscription, not migrate it. See session-hygiene.ts's
        // module docstring / docs/BRIDGE_PROTOCOL.md §0(n).
        const sessions = [
            session({ sessionId: 1, createdAt: NOW - SESSION_MAX_AGE_MS * 10, subscriptionCount: 1 }),
        ];
        assert.deepEqual(rotatableSessions(sessions, NOW), []);
    });

    it("does not close a subscription-free session inside the age ceiling", () => {
        const sessions = [session({ sessionId: 1, createdAt: NOW - (SESSION_MAX_AGE_MS - 1) })];
        assert.deepEqual(rotatableSessions(sessions, NOW), []);
    });

    it("respects a custom age ceiling", () => {
        const sessions = [session({ sessionId: 1, createdAt: NOW - 3_600_000 })];
        assert.equal(rotatableSessions(sessions, NOW, 3_600_000).length, 1);
        assert.equal(rotatableSessions(sessions, NOW, 3_600_001).length, 0);
    });
});

describe("periodicSweep (issue #283 items 2+3 combined)", () => {
    it("reports a session that is both dead and past the age ceiling ONCE, as dead", () => {
        const sessions = [
            session({
                sessionId: 1,
                createdAt: NOW - SESSION_MAX_AGE_MS * 2,
                activeTimestamp: NOW - DEAD_SESSION_QUIET_MS,
            }),
        ];
        const closures = periodicSweep(sessions, NOW);
        assert.equal(closures.length, 1);
        assert.equal(closures[0]!.reason, "dead");
    });

    it("closes an independent dead session and an independent rotated session, both", () => {
        const sessions = [
            session({ sessionId: 1, activeTimestamp: NOW - DEAD_SESSION_QUIET_MS }), // dead, young
            session({ sessionId: 2, createdAt: NOW - SESSION_MAX_AGE_MS, activeTimestamp: NOW }), // old, active
        ];
        const closures = periodicSweep(sessions, NOW);
        const byId = new Map(closures.map(c => [c.sessionId, c.reason]));
        assert.equal(byId.get(1), "dead");
        assert.equal(byId.get(2), "rotated");
    });

    it("closes nothing for a healthy, young, subscribed session", () => {
        const sessions = [session({ sessionId: 1, subscriptionCount: 1 })];
        assert.deepEqual(periodicSweep(sessions, NOW), []);
    });
});

describe("peerSessionCounts (issue #283 item 5)", () => {
    it("groups by peer+fabric and sorts by peer id", () => {
        const sessions = [
            session({ sessionId: 1, peerNodeId: ECHO }),
            session({ sessionId: 2, peerNodeId: ECHO }),
            session({ sessionId: 3, peerNodeId: OTHER_ECHO }),
        ];
        const counts = peerSessionCounts(sessions);
        assert.deepEqual(counts, [
            { peerNodeId: ECHO, fabricIndex: FABRIC, liveSessions: 2 },
            { peerNodeId: OTHER_ECHO, fabricIndex: FABRIC, liveSessions: 1 },
        ]);
    });

    it("keeps two peers on different fabrics apart even with the same node id", () => {
        const sessions = [
            session({ sessionId: 1, peerNodeId: ECHO, fabricIndex: 1 }),
            session({ sessionId: 2, peerNodeId: ECHO, fabricIndex: 2 }),
        ];
        assert.equal(peerSessionCounts(sessions).length, 2);
    });

    it("is empty for no sessions", () => {
        assert.deepEqual(peerSessionCounts([]), []);
    });
});
