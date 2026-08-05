/**
 * Minimal test client: a WebSocket wrapped in a frame queue so tests can await
 * one frame at a time in receipt order (§1 guarantees ordering).
 */

import { WebSocket } from "ws";

export class TestClient {
    readonly #socket: WebSocket;
    readonly #frames: Record<string, unknown>[] = [];
    #waiter?: (frame: Record<string, unknown>) => void;
    closed = false;

    private constructor(socket: WebSocket) {
        this.#socket = socket;
        socket.on("message", data => {
            const frame = JSON.parse(data.toString()) as Record<string, unknown>;
            const waiter = this.#waiter;
            if (waiter !== undefined) {
                this.#waiter = undefined;
                waiter(frame);
            } else {
                this.#frames.push(frame);
            }
        });
        socket.on("close", () => {
            this.closed = true;
        });
    }

    static async connect(port: number): Promise<TestClient> {
        const socket = new WebSocket(`ws://127.0.0.1:${port}`);
        // Listeners must be attached before `open` resolves: the node sends the
        // handshake the instant it accepts, and `ws` drops messages emitted
        // before a listener exists.
        const client = new TestClient(socket);
        await new Promise<void>((resolve, reject) => {
            socket.once("open", resolve);
            socket.once("error", reject);
        });
        return client;
    }

    /** Next frame, or a rejection after {@link timeoutMs}. */
    async next(timeoutMs = 2000): Promise<Record<string, unknown>> {
        const buffered = this.#frames.shift();
        if (buffered !== undefined) {
            return buffered;
        }
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this.#waiter = undefined;
                reject(new Error("Timed out waiting for a frame"));
            }, timeoutMs);
            this.#waiter = frame => {
                clearTimeout(timer);
                resolve(frame);
            };
        });
    }

    send(frame: unknown): void {
        this.#socket.send(JSON.stringify(frame));
    }

    /** Bypass JSON.stringify — for the garbage-frame tests. */
    sendRaw(payload: string | Buffer): void {
        this.#socket.send(payload);
    }

    /** True once no more frames are buffered — used to assert "nothing came back". */
    get buffered(): number {
        return this.#frames.length;
    }

    /**
     * Send a request and return its response frame.
     *
     * `timeoutMs` is worth raising for §3.10: `ServerNode.erase()` takes the
     * Matter stack offline, wipes its storage and brings it back up, which is
     * comfortably longer than the 2s a local handler normally needs.
     */
    async request(frame: unknown, timeoutMs?: number): Promise<Record<string, unknown>> {
        this.send(frame);
        return this.next(timeoutMs);
    }

    async waitForClose(timeoutMs = 2000): Promise<void> {
        if (this.closed) {
            return;
        }
        await new Promise<void>((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("Socket did not close")), timeoutMs);
            this.#socket.once("close", () => {
                clearTimeout(timer);
                resolve();
            });
        });
    }

    close(): void {
        this.#socket.close();
    }
}
