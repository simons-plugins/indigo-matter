/**
 * Issue #286 — the pure churn detector, on an injected clock.
 *
 * No matter.js here by design (`churn.ts` imports none): the states worth
 * testing take three controller session generations and half an hour of real
 * Alexa misbehaviour to reach, which is exactly why the decision was pulled out
 * of `node.ts` in the first place. `node.ts`'s share is the wiring, pinned in
 * `persistence.test.ts` against a real started node.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
    CHURN_DELETION_THRESHOLD,
    CHURN_MIN_PILED_SESSIONS,
    CHURN_SESSION_THRESHOLD,
    CHURN_WINDOW_MINUTES,
    ChurnDetector,
    churnWarning,
} from "../src/churn.js";

/** The #283 peer, in the hex the bridge reports it as. */
const ECHO = "41869fbd537ef01";
const OTHER_ECHO = "9f2c00114b3d201";
const ALEXA_FABRIC = 2;

const MINUTE = 60_000;

/** A clock the test moves by hand — `window.test.ts`'s idiom. */
function fakeClock(start = 1_700_000_000_000): { now: () => number; advance: (ms: number) => void } {
    let time = start;
    return { now: () => time, advance: ms => { time += ms; } };
}

/**
 * Give `peer` `n` live sessions. Ids are derived from the peer so two peers
 * cannot collide, and re-calling with a larger `n` tops up rather than resets.
 */
function pileSessions(detector: ChurnDetector, peer: string, n: number, fabric = ALEXA_FABRIC): void {
    for (let i = 0; i < n; i++) {
        detector.sessionOpened(sessionId(peer, fabric, i), peer, fabric);
    }
}

function sessionId(peer: string, fabric: number, index: number): number {
    return (peer === ECHO ? 1000 : 2000) + fabric * 100 + index;
}

/** `n` terminated-subscription deletions for one peer, `gapMs` apart. */
function churnFor(
    detector: ChurnDetector,
    clock: { advance: (ms: number) => void },
    peer: string,
    n: number,
    gapMs = MINUTE,
): void {
    for (let i = 0; i < n; i++) {
        detector.subscriptionRemoved(peer, ALEXA_FABRIC, true);
        clock.advance(gapMs);
    }
}

describe("ChurnDetector (issue #286)", () => {
    it("flags a peer once its terminated deletions reach the threshold", () => {
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        // Below the session threshold, so it is the DELETION arm under test.
        pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);

        churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD - 1);
        assert.equal(detector.verdict().active, false, "under the threshold is not churn");

        churnFor(detector, clock, ECHO, 1);
        const verdict = detector.verdict();
        assert.equal(verdict.checked, true);
        assert.equal(verdict.active, true);
        assert.deepEqual(verdict.peers.map(peer => peer.peerNodeId), [ECHO]);
        assert.equal(verdict.peers[0]?.invalidDeletions, CHURN_DELETION_THRESHOLD);
        assert.equal(verdict.peers[0]?.windowMinutes, CHURN_WINDOW_MINUTES);
    });

    it("does NOT flag three terminated deletions from a peer holding ONE session", () => {
        // The review's adversarial case, and the reason the deletion arm is
        // conjunctive. `isTerminated` is also set by handlePeerCancel() on
        // every routine keepSubscriptions:false re-subscribe, and by the
        // transient-network give-up branch — so a healthy Echo re-subscribing
        // three times, or riding out three Wi-Fi blips, lands exactly here.
        // Telling that user to restart a working bridge is the failure mode.
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        pileSessions(detector, ECHO, 1);
        churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD * 2);

        const verdict = detector.verdict();
        assert.equal(verdict.active, false, "one session is a controller reconnecting, not a pile");
        assert.equal(verdict.checked, true, "…and that is a real all-clear, not an absence");
    });

    it("ignores ordinary unsubscribes — only TERMINATED deletions count", () => {
        // Piled sessions present, so this fails if the wasTerminated filter is
        // dropped rather than passing because the conjunction was unmet.
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
        for (let i = 0; i < CHURN_DELETION_THRESHOLD * 3; i++) {
            detector.subscriptionRemoved(ECHO, ALEXA_FABRIC, false);
            clock.advance(MINUTE);
        }
        assert.equal(detector.verdict().active, false);
    });

    it("flags a peer on piled-up live sessions with no deletions seen at all", () => {
        // The bridge-restarted-mid-fault case: the deletions happened before
        // anyone was counting, but the sessions they left behind are still here.
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        for (let i = 0; i < CHURN_SESSION_THRESHOLD; i++) {
            detector.sessionOpened(100 + i, ECHO, ALEXA_FABRIC);
        }
        const verdict = detector.verdict();
        assert.equal(verdict.active, true);
        assert.equal(verdict.peers[0]?.liveSessions, CHURN_SESSION_THRESHOLD);
        assert.equal(verdict.peers[0]?.invalidDeletions, 0);
    });

    it("counts sessions per peer, so one Echo churning does not accuse the other", () => {
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        for (let i = 0; i < CHURN_SESSION_THRESHOLD; i++) {
            detector.sessionOpened(200 + i, ECHO, ALEXA_FABRIC);
        }
        // Over the deletion threshold but holding ONE session: innocent.
        detector.sessionOpened(300, OTHER_ECHO, ALEXA_FABRIC);
        churnFor(detector, clock, OTHER_ECHO, CHURN_DELETION_THRESHOLD);

        const verdict = detector.verdict();
        assert.deepEqual(verdict.peers.map(peer => peer.peerNodeId), [ECHO],
            "the quiet peer must not be named in a notice the user is asked to act on");
    });

    it("separates the same node id on two fabrics", () => {
        const detector = new ChurnDetector({ now: fakeClock().now });
        for (let i = 0; i < CHURN_SESSION_THRESHOLD; i++) {
            detector.sessionOpened(400 + i, ECHO, ALEXA_FABRIC);
            detector.sessionOpened(500 + i, ECHO, 1);
        }
        const verdict = detector.verdict();
        assert.deepEqual(
            verdict.peers.map(peer => peer.fabricIndex).sort(),
            [1, ALEXA_FABRIC],
            "two fabrics are two controllers even at the same node id",
        );
    });

    it("closing sessions drops the peer back under the session threshold", () => {
        const detector = new ChurnDetector({ now: fakeClock().now });
        for (let i = 0; i < CHURN_SESSION_THRESHOLD; i++) {
            detector.sessionOpened(600 + i, ECHO, ALEXA_FABRIC);
        }
        assert.equal(detector.verdict().active, true);
        detector.sessionClosed(600);
        assert.equal(detector.verdict().active, false, "a reaped session is a recovered peer");
    });

    it("self-clears when the rolling window drains", () => {
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
        churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
        assert.equal(detector.verdict().active, true);

        clock.advance(CHURN_WINDOW_MINUTES * MINUTE);
        const verdict = detector.verdict();
        assert.equal(verdict.active, false, "a fault that stopped an hour ago is not a current fault");
        assert.equal(verdict.checked, true);
        assert.deepEqual(verdict.peers, []);
    });

    it("holds `since` at the FIRST crossing while the peer stays over threshold", () => {
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
        churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
        const first = detector.verdict().peers[0]?.since;
        assert.ok(first !== undefined);

        clock.advance(5 * MINUTE);
        churnFor(detector, clock, ECHO, 1);
        assert.equal(detector.verdict().peers[0]?.since, first,
            "`since` is when the churn started, not when it was last seen");
    });

    it("restarts `since` after the peer has genuinely recovered", () => {
        const clock = fakeClock();
        const detector = new ChurnDetector({ now: clock.now });
        pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
        churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
        const first = detector.verdict().peers[0]?.since;

        clock.advance(CHURN_WINDOW_MINUTES * MINUTE);
        assert.equal(detector.verdict().active, false);

        churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
        assert.notEqual(detector.verdict().peers[0]?.since, first, "a new episode is a new `since`");
    });

    describe("degradation (checked:false is not healthy)", () => {
        it("reports checked:false for good once broken, whatever arrives afterwards", () => {
            const clock = fakeClock();
            const detector = new ChurnDetector({ now: clock.now });
            pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
            churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
            assert.equal(detector.verdict().active, true);

            detector.markBroken();
            const broken = detector.verdict();
            assert.deepEqual(broken, { checked: false, active: false, peers: [] });

            // Everything a healthy detector would have called churn.
            for (let i = 0; i < CHURN_SESSION_THRESHOLD; i++) {
                detector.sessionOpened(700 + i, ECHO, ALEXA_FABRIC);
            }
            churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD * 2);
            clock.advance(CHURN_WINDOW_MINUTES * MINUTE);

            assert.deepEqual(detector.verdict(), { checked: false, active: false, peers: [] },
                "a detector that missed events must never claim to have checked again");
            assert.equal(detector.isBroken, true);
        });
    });

    describe("poll (the 15s watchdog re-fire guard)", () => {
        it("calls a healthy first poll no transition at all", () => {
            const detector = new ChurnDetector({ now: fakeClock().now });
            assert.equal(detector.poll().changed, false,
                "a quiet bridge's first status read must not log a recovery that never happened");
        });

        it("reports one transition into churn and none while it persists", () => {
            const clock = fakeClock();
            const detector = new ChurnDetector({ now: clock.now });
            pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
            churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);

            assert.equal(detector.poll().changed, true, "the crossing is the news");
            assert.equal(detector.poll().changed, false, "the same standing fault, 15s later");

            // A fourth deletion moves the count but not the situation.
            churnFor(detector, clock, ECHO, 1);
            const again = detector.poll();
            assert.equal(again.changed, false, "counts moving is not a transition");
            assert.equal(again.verdict.peers[0]?.invalidDeletions, CHURN_DELETION_THRESHOLD + 1,
                "…but the report still carries the current count");
        });

        it("reports the recovery, then falls quiet again", () => {
            const clock = fakeClock();
            const detector = new ChurnDetector({ now: clock.now });
            pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
            churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
            detector.poll();

            clock.advance(CHURN_WINDOW_MINUTES * MINUTE);
            assert.equal(detector.poll().changed, true, "the clear is news too");
            assert.equal(detector.poll().changed, false);
        });

        it("reports a second peer joining as a transition", () => {
            const clock = fakeClock();
            const detector = new ChurnDetector({ now: clock.now });
            pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
            pileSessions(detector, OTHER_ECHO, CHURN_MIN_PILED_SESSIONS);
            churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
            detector.poll();

            churnFor(detector, clock, OTHER_ECHO, CHURN_DELETION_THRESHOLD);
            const poll = detector.poll();
            assert.equal(poll.changed, true, "which peers are churning is the actionable part");
            assert.equal(poll.verdict.peers.length, 2);
        });

        it("reports breaking as a transition, so the notice gets cleared", () => {
            const clock = fakeClock();
            const detector = new ChurnDetector({ now: clock.now });
            pileSessions(detector, ECHO, CHURN_MIN_PILED_SESSIONS);
            churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD);
            detector.poll();

            detector.markBroken();
            assert.equal(detector.poll().changed, true);
            assert.equal(detector.poll().changed, false);
        });
    });

    it("names the peer and the fabric in the notice", () => {
        const clock = fakeClock(Date.parse("2026-08-23T09:12:00.000Z"));
        const detector = new ChurnDetector({ now: clock.now });
        for (let i = 0; i < 5; i++) {
            detector.sessionOpened(800 + i, ECHO, ALEXA_FABRIC);
        }
        churnFor(detector, clock, ECHO, CHURN_DELETION_THRESHOLD, 0);

        assert.equal(
            churnWarning(detector.verdict()),
            "Subscription churn detected for controller peer 41869fbd537ef01 (fabric 2): 3 invalid "
            + "subscription deletion(s) in 30 min, 5 live session(s) since 2026-08-23T09:12:00.000Z "
            + "— restart the Matter bridge to recover.",
            "byte-identical to the golden get_status_churning warning",
        );
    });
});
