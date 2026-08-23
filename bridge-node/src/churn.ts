/**
 * Controller subscription-churn detection (issue #286).
 *
 * Alexa opens a CASE session, subscribes, then some minutes later reports the
 * subscription invalid; matter.js terminates and deletes it, Alexa re-subscribes
 * over a NEW session, and the old sessions are never reaped. Measured on the
 * reference server (issue #283, 2026-08-23): three session generations for one
 * Echo peer inside 30 minutes, a ~30-minute re-subscribe cycle, 24
 * SubscribeRequests a day. Nothing recovers it but a bridge restart, and until
 * this detector existed the only evidence was a log line nobody reads.
 *
 * **Detection is read-only.** Sessions are matter.js's to manage; this module
 * counts and reports, and never closes anything.
 *
 * Deliberately pure — no matter.js import, no timers, injected clock — for the
 * same reason `refuseReasonFor` is (ADR/ARCHITECTURE): the interesting states
 * take three session generations and half an hour of real controller
 * misbehaviour to reach, so they are unreachable in a test that needs hardware.
 * {@link BridgeNode} owns the thin wiring from the matter.js observables.
 *
 * The signature is deliberately NOT the "Subscription reported invalid by peer"
 * log line. That string belongs to a dependency and is one of three causes that
 * set `Subscription.isTerminated`; what identifies the fault is the *rate* of
 * terminated-subscription deletions for one peer alongside the pile of live
 * sessions that peer is holding open.
 */

import type { ChurnPeer, SubscriptionChurn } from "./protocol.js";

/**
 * Rolling window for the deletion count.
 *
 * 30 minutes because that is the observed re-subscribe cycle (#283): a window
 * shorter than one cycle could never see two deletions, and a much longer one
 * would keep reporting a fault that has already stopped.
 */
export const CHURN_WINDOW_MINUTES = 30;

/**
 * Terminated-subscription deletions for ONE peer inside the window that mean
 * churn. Three, because the observed fault produced exactly three generations
 * in 30 minutes and a healthy controller produces none — one deletion is an
 * ordinary reconnect, two is a coincidence worth ignoring.
 */
export const CHURN_DELETION_THRESHOLD = 3;

/**
 * Live CASE sessions for ONE peer that mean churn on their own.
 *
 * A controller normally holds one or two (Alexa uses a second briefly while
 * re-subscribing). Four is the pile that only accumulates when the old ones are
 * never reaped, and it catches the case where the deletions happened before the
 * bridge started counting.
 */
export const CHURN_SESSION_THRESHOLD = 4;

export interface ChurnDetectorOptions {
    /** Injected for tests; milliseconds since the epoch. */
    now?: () => number;
    windowMinutes?: number;
    deletionThreshold?: number;
    sessionThreshold?: number;
}

/** What one poll saw, and whether it is news. */
export interface ChurnPoll {
    verdict: SubscriptionChurn;
    /**
     * True only when this verdict differs from the previous poll's in a way a
     * user needs telling about — `checked` flipped, `active` flipped, or the
     * set of over-threshold peers changed. Counts alone do not make it true:
     * `get_status` is the plugin's 15s watchdog tick, and a caller that logged
     * on every change would re-log one standing fault four times a minute.
     */
    changed: boolean;
}

interface PeerState {
    peerNodeId: string;
    fabricIndex: number;
    /** Session ids currently open for this peer. */
    sessions: Set<number>;
    /** When each terminated-subscription deletion landed, oldest first. */
    deletions: number[];
    /** When this peer last crossed a threshold; undefined while under one. */
    since?: number;
}

/**
 * Per-peer churn bookkeeping.
 *
 * Fed plain events by {@link BridgeNode}; answers {@link verdict} on demand.
 * The rolling window is pruned lazily at read time (and on insert, so an
 * unpolled detector cannot grow without bound) rather than by a timer — there
 * is nothing to do when it drains except answer differently next time.
 */
export class ChurnDetector {
    readonly #now: () => number;
    readonly #windowMs: number;
    readonly #deletionThreshold: number;
    readonly #sessionThreshold: number;
    readonly #peers = new Map<string, PeerState>();
    /** `sessionId → peer key`, so a close needs only the id matter.js gives it. */
    readonly #sessionPeers = new Map<number, string>();
    #broken = false;
    /**
     * The last {@link poll} signature, for the transition test. Seeded with the
     * healthy one a fresh detector is genuinely in, so the first poll of a
     * quiet bridge is not itself a transition.
     */
    #signature = signatureOf({ checked: true, active: false, peers: [] });

    constructor(options: ChurnDetectorOptions = {}) {
        this.#now = options.now ?? Date.now;
        this.#windowMs = (options.windowMinutes ?? CHURN_WINDOW_MINUTES) * 60_000;
        this.#deletionThreshold = options.deletionThreshold ?? CHURN_DELETION_THRESHOLD;
        this.#sessionThreshold = options.sessionThreshold ?? CHURN_SESSION_THRESHOLD;
    }

    /**
     * The detector can no longer observe session state, so every verdict from
     * here on is `checked: false`.
     *
     * One-way on purpose. A detector that had its wiring fail, or a handler
     * that threw, has an unknown number of unseen events behind it — and the
     * whole point of `checked` is that a bridge which cannot look must not
     * report the healthy answer. Later events are ignored rather than counted
     * into a total that is already missing some.
     */
    markBroken(): void {
        this.#broken = true;
        this.#peers.clear();
        this.#sessionPeers.clear();
    }

    get isBroken(): boolean {
        return this.#broken;
    }

    sessionOpened(sessionId: number, peerNodeId: string, fabricIndex: number): void {
        if (this.#broken) {
            return;
        }
        const key = peerKey(peerNodeId, fabricIndex);
        this.#sessionPeers.set(sessionId, key);
        this.#peer(key, peerNodeId, fabricIndex).sessions.add(sessionId);
    }

    sessionClosed(sessionId: number): void {
        if (this.#broken) {
            return;
        }
        const key = this.#sessionPeers.get(sessionId);
        if (key === undefined) {
            return;
        }
        this.#sessionPeers.delete(sessionId);
        this.#peers.get(key)?.sessions.delete(sessionId);
    }

    /**
     * A subscription left a peer's session. Only `wasTerminated` ones count:
     * an ordinary unsubscribe is how a controller says goodbye, and counting it
     * would flag every well-behaved ecosystem that ever reconnects.
     */
    subscriptionRemoved(peerNodeId: string, fabricIndex: number, wasTerminated: boolean): void {
        if (this.#broken || !wasTerminated) {
            return;
        }
        const now = this.#now();
        const peer = this.#peer(peerKey(peerNodeId, fabricIndex), peerNodeId, fabricIndex);
        peer.deletions.push(now);
        prune(peer, now - this.#windowMs);
    }

    /** The verdict now, with the rolling window pruned to `nowMs`. */
    verdict(nowMs: number = this.#now()): SubscriptionChurn {
        if (this.#broken) {
            return { checked: false, active: false, peers: [] };
        }
        const cutoff = nowMs - this.#windowMs;
        const peers: ChurnPeer[] = [];
        for (const [key, peer] of this.#peers) {
            prune(peer, cutoff);
            const over = peer.deletions.length >= this.#deletionThreshold
                || peer.sessions.size >= this.#sessionThreshold;
            if (over) {
                peer.since ??= nowMs;
                peers.push({
                    peerNodeId: peer.peerNodeId,
                    fabricIndex: peer.fabricIndex,
                    liveSessions: peer.sessions.size,
                    invalidDeletions: peer.deletions.length,
                    windowMinutes: this.#windowMs / 60_000,
                    since: new Date(peer.since).toISOString(),
                });
                continue;
            }
            peer.since = undefined;
            if (peer.sessions.size === 0 && peer.deletions.length === 0) {
                this.#peers.delete(key);
            }
        }
        peers.sort((a, b) => a.peerNodeId.localeCompare(b.peerNodeId));
        return { checked: true, active: peers.length > 0, peers };
    }

    /** {@link verdict}, plus whether it is a transition worth acting on. */
    poll(nowMs: number = this.#now()): ChurnPoll {
        const verdict = this.verdict(nowMs);
        const signature = signatureOf(verdict);
        const changed = signature !== this.#signature;
        this.#signature = signature;
        return { verdict, changed };
    }

    #peer(key: string, peerNodeId: string, fabricIndex: number): PeerState {
        let peer = this.#peers.get(key);
        if (peer === undefined) {
            peer = { peerNodeId, fabricIndex, sessions: new Set(), deletions: [] };
            this.#peers.set(key, peer);
        }
        return peer;
    }
}

/**
 * The §4.3 notice text for an active verdict.
 *
 * Names the peer because the two Echoes on one fabric are not interchangeable:
 * "an Alexa controller is churning" leaves the user nothing to act on, and the
 * only recovery is a bridge restart.
 */
export function churnWarning(churn: SubscriptionChurn): string {
    const peers = churn.peers.map(peer =>
        `controller peer ${peer.peerNodeId} (fabric ${peer.fabricIndex}): ${peer.invalidDeletions} invalid `
        + `subscription deletion(s) in ${peer.windowMinutes} min, ${peer.liveSessions} live session(s) `
        + `since ${peer.since}`);
    return `Subscription churn detected for ${peers.join("; ")} — restart the Matter bridge to recover.`;
}

/**
 * What makes one verdict a different *situation* from another. Deliberately
 * excludes the counts: those move on every deletion, and a caller that treated
 * that as news would re-report one standing fault forever.
 */
function signatureOf(churn: SubscriptionChurn): string {
    return [
        churn.checked,
        churn.active,
        ...churn.peers.map(peer => `${peer.fabricIndex}/${peer.peerNodeId}`),
    ].join("|");
}

function peerKey(peerNodeId: string, fabricIndex: number): string {
    return `${fabricIndex}/${peerNodeId}`;
}

/** Drop deletions older than `cutoff`; the array is kept oldest-first. */
function prune(peer: PeerState, cutoff: number): void {
    let keep = 0;
    while (keep < peer.deletions.length && (peer.deletions[keep] as number) <= cutoff) {
        keep++;
    }
    if (keep > 0) {
        peer.deletions.splice(0, keep);
    }
}
