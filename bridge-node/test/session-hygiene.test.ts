/**
 * Issue #283 "Finding 2" — the pure session-hygiene decisions, on plain
 * descriptors and a hand-moved clock. No matter.js here by design
 * (`session-hygiene.ts` imports none) — `persistence.test.ts` pins the
 * `node.ts` wiring (peer-scoping the sweep, closing real sessions) against a
 * real started node, the same split `churn.ts`/`churn.test.ts` established.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import * as hygiene from "../src/session-hygiene.js";
import {
    peerSessionCounts,
    type SessionDescriptor,
    supersededSessions,
} from "../src/session-hygiene.js";

const ECHO = "41869fbd537ef01";
const OTHER_ECHO = "9f2c00114b3d201";
const FABRIC = 2;
const NOW = 1_700_000_000_000;
const SIXTY_DAYS = 60 * 24 * 60 * 60 * 1000;

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

describe("supersededSessions (issue #283, the superseded sweep)", () => {
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

    it("closes a superseded session even while it holds a live subscription — deliberately: the peer holds the replacement", () => {
        // The bet: `justOpened` (session 2) IS the replacement the controller
        // will re-subscribe over, so orphaning session 1's subscription here
        // is the intended outcome — not an oversight `subscriptionCount`
        // should have guarded against.
        const peerSessions = [
            session({ sessionId: 1, createdAt: NOW - 10_000, subscriptionCount: 1 }),
            session({ sessionId: 2, createdAt: NOW }),
        ];
        const closures = supersededSessions(peerSessions, 2);
        assert.deepEqual(closures.map(c => c.sessionId), [1]);
    });
});

describe("no quiet/aged-session close decision exists (Alexa 15 s stall regression)", () => {
    it("exports no dead/rotated/periodic closer — superseded is the only reason a session is ever closed", () => {
        // `initiateForceClose` sends the peer no CloseSession, so a close
        // decided from quiet-time or age alone silently drops a session a
        // polling controller still believes in. See the module docstring.
        for (const removed of [
            "deadSessions",
            "rotatableSessions",
            "periodicSweep",
            "DEAD_SESSION_QUIET_MS",
            "SESSION_MAX_AGE_MS",
        ]) {
            assert.equal(removed in hygiene, false, `${removed} must stay removed`);
        }
    });

    it("supersededSessions never selects a lone session, however quiet or old", () => {
        // Far beyond any plausible quiet/age threshold (60 days each), so a
        // mutation that merely raised the old 60 s / 4 h cut-offs still fails.
        const lone = session({
            sessionId: 1,
            createdAt: NOW - SIXTY_DAYS,
            activeTimestamp: NOW - SIXTY_DAYS,
        });
        assert.deepEqual(supersededSessions([lone], 1), []);
    });

    it("selects a quiet-and-old session only because a NEW session from the same peer superseded it", () => {
        // Documents the intentional interaction: age and quiet time never
        // choose a session, but a replacement from the same peer+fabric does.
        const stale = session({
            sessionId: 1,
            createdAt: NOW - SIXTY_DAYS,
            activeTimestamp: NOW - SIXTY_DAYS,
        });
        const fresh = session({ sessionId: 2, createdAt: NOW });
        assert.deepEqual(supersededSessions([stale, fresh], 2).map(c => c.sessionId), [1]);
    });

    it("selects nothing from a subscribed session and a quiet subscription-free sibling absent a new session", () => {
        // `justOpenedSessionId` is the sweep's only trigger: with no new
        // session in the set there is nothing to supersede either sibling.
        const subscribed = session({ sessionId: 1, subscriptionCount: 1, createdAt: NOW - SIXTY_DAYS });
        const quietSibling = session({
            sessionId: 2,
            createdAt: NOW - SIXTY_DAYS,
            activeTimestamp: NOW - SIXTY_DAYS,
        });
        assert.deepEqual(supersededSessions([subscribed, quietSibling], 999), []);
    });
});

describe("peerSessionCounts (issue #283, the per-peer diagnostic)", () => {
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
