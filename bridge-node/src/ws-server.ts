/**
 * The plugin ⇄ bridge-node WebSocket server (BRIDGE_PROTOCOL.md).
 *
 * Loopback only: there is no auth on this socket because there is no remote
 * surface (§6.1). No matter.js import here — the server talks to a
 * {@link BridgeFacade}, which is what makes the protocol testable without a
 * live Matter stack.
 */

import { WebSocketServer, type WebSocket } from "ws";

import {
    type BridgeFacade,
    ErrorCode,
    type ErrorCodeValue,
    type HandshakeFrame,
    PROTOCOL_VERSION,
    ProtocolError,
    UNATTACHED_TIMEOUT_MS,
} from "./protocol.js";

export const LOOPBACK_HOST = "127.0.0.1";

export interface BridgeWsServerOptions {
    port: number;
    bridge: BridgeFacade;
    bridgeVersion: string;
    matterJsVersion: string;
    log?: (message: string) => void;
    /** Overridable so the §2 timeout is testable without a 10s wait. */
    unattachedTimeoutMs?: number;
}

interface ClientState {
    attached: boolean;
    unattachedTimer?: NodeJS.Timeout;
}

/** Commands whose handlers exist in E0. Endpoint CRUD arrives in E1. */
type CommandHandler = (args: Record<string, unknown>, socket: WebSocket, state: ClientState) => Promise<unknown>;

export class BridgeWsServer {
    #wss?: WebSocketServer;
    #attached?: WebSocket;
    readonly #clients = new Map<WebSocket, ClientState>();
    readonly #handlers = new Map<string, CommandHandler>();
    readonly #log: (message: string) => void;

    constructor(private readonly options: BridgeWsServerOptions) {
        this.#log = options.log ?? console.log;
        this.#handlers.set("attach", (args, socket, state) => this.handleAttach(args, socket, state));
        this.#handlers.set("get_status", async () => this.options.bridge.getStatus());
        this.#handlers.set("get_pairing", async () => this.options.bridge.getPairing());
        this.#handlers.set("open_commissioning_window", async args => this.handleOpenWindow(args));
    }

    /** Bind and start accepting connections. Resolves once listening. */
    async listen(): Promise<void> {
        await new Promise<void>((resolve, reject) => {
            const wss = new WebSocketServer({ host: LOOPBACK_HOST, port: this.options.port });
            wss.once("error", reject);
            wss.once("listening", () => {
                wss.off("error", reject);
                wss.on("error", error => this.#log(`WebSocket server error: ${describeError(error)}`));
                resolve();
            });
            wss.on("connection", socket => this.onConnection(socket));
            this.#wss = wss;
        });
        this.#log(`Protocol WebSocket listening on ws://${LOOPBACK_HOST}:${this.options.port}`);
    }

    /** The bound port — useful when a test asks for port 0. */
    get port(): number {
        const address = this.#wss?.address();
        if (address === undefined || address === null || typeof address === "string") {
            return this.options.port;
        }
        return address.port;
    }

    async close(): Promise<void> {
        const wss = this.#wss;
        this.#wss = undefined;
        for (const [socket, state] of this.#clients) {
            clearTimeout(state.unattachedTimer);
            socket.close();
        }
        this.#clients.clear();
        this.#attached = undefined;
        if (wss !== undefined) {
            await new Promise<void>(resolve => wss.close(() => resolve()));
        }
    }

    private onConnection(socket: WebSocket): void {
        const state: ClientState = { attached: false };
        this.#clients.set(socket, state);

        // §2 step 1: the bare handshake frame, before anything else.
        const handshake: HandshakeFrame = {
            protocolVersion: PROTOCOL_VERSION,
            bridgeVersion: this.options.bridgeVersion,
            matterJsVersion: this.options.matterJsVersion,
        };
        socket.send(JSON.stringify(handshake));

        // §2: a connection that handshakes but never attaches is closed.
        const timeoutMs = this.options.unattachedTimeoutMs ?? UNATTACHED_TIMEOUT_MS;
        state.unattachedTimer = setTimeout(() => {
            if (!state.attached) {
                this.#log(`Closing connection that did not attach within ${timeoutMs}ms`);
                socket.close();
            }
        }, timeoutMs);
        state.unattachedTimer.unref?.();

        socket.on("message", data => {
            void this.onMessage(socket, state, data.toString());
        });
        socket.on("close", () => {
            clearTimeout(state.unattachedTimer);
            this.#clients.delete(socket);
            if (this.#attached === socket) {
                this.#attached = undefined;
                this.#log("Attached client disconnected");
            }
        });
        socket.on("error", error => this.#log(`Socket error: ${describeError(error)}`));
    }

    private async onMessage(socket: WebSocket, state: ClientState, raw: string): Promise<void> {
        let frame: unknown;
        try {
            frame = JSON.parse(raw);
        } catch {
            // Unaddressable: no message_id to echo, so there is nobody to answer.
            this.#log("Dropping unparseable frame");
            return;
        }

        if (typeof frame !== "object" || frame === null) {
            this.#log("Dropping non-object frame");
            return;
        }

        const { message_id: messageId, command, args } = frame as Record<string, unknown>;
        if (typeof messageId !== "string") {
            this.#log("Dropping frame without a message_id");
            return;
        }
        if (typeof command !== "string") {
            this.send(socket, { message_id: messageId, error_code: ErrorCode.malformedArgs, details: "Missing command" });
            return;
        }

        const commandArgs: Record<string, unknown> =
            typeof args === "object" && args !== null ? (args as Record<string, unknown>) : {};

        const handler = this.#handlers.get(command);
        if (handler === undefined) {
            this.sendError(socket, messageId, ErrorCode.unknownCommand, `Unknown command ${command}`);
            return;
        }
        if (command !== "attach" && !state.attached) {
            this.sendError(socket, messageId, ErrorCode.notAttached, `${command} requires a successful attach first`);
            return;
        }

        try {
            const result = await handler(commandArgs, socket, state);
            this.send(socket, { message_id: messageId, result });
        } catch (error) {
            if (error instanceof ProtocolError) {
                this.sendError(socket, messageId, error.code, error.message);
                // §2: version skew fails closed — answer, then hang up.
                if (error.code === ErrorCode.versionMismatch) {
                    socket.close();
                }
                return;
            }
            this.#log(`Command ${command} failed: ${describeError(error)}`);
            this.sendError(socket, messageId, ErrorCode.internal, describeError(error));
        }
    }

    private async handleAttach(
        args: Record<string, unknown>,
        socket: WebSocket,
        state: ClientState,
    ): Promise<unknown> {
        const version = args.protocolVersion;
        if (typeof version !== "number") {
            throw new ProtocolError(ErrorCode.malformedArgs, "attach requires a numeric protocolVersion");
        }
        if (version !== PROTOCOL_VERSION) {
            throw new ProtocolError(
                ErrorCode.versionMismatch,
                `Node speaks protocol version ${PROTOCOL_VERSION}, client sent ${version}`,
            );
        }

        // §2: exactly one attached client; a new attach supersedes the incumbent,
        // which is how we recover from a half-open socket left by a plugin crash.
        const incumbent = this.#attached;
        if (incumbent !== undefined && incumbent !== socket) {
            this.#log("Superseding previously attached client");
            incumbent.close();
            const incumbentState = this.#clients.get(incumbent);
            if (incumbentState !== undefined) {
                incumbentState.attached = false;
            }
        }

        state.attached = true;
        clearTimeout(state.unattachedTimer);
        state.unattachedTimer = undefined;
        this.#attached = socket;

        const pluginVersion = typeof args.pluginVersion === "string" ? args.pluginVersion : "unknown";
        this.#log(`Client attached (plugin ${pluginVersion})`);

        // E1 reconciles `args.endpoints` here; E0 serves a fixed endpoint set.
        return this.options.bridge.getStatus();
    }

    private async handleOpenWindow(args: Record<string, unknown>): Promise<unknown> {
        const duration = args.durationSeconds ?? 900;
        if (typeof duration !== "number" || !Number.isInteger(duration) || duration <= 0) {
            throw new ProtocolError(ErrorCode.malformedArgs, "durationSeconds must be a positive integer");
        }
        return this.options.bridge.openCommissioningWindow(duration);
    }

    private sendError(socket: WebSocket, messageId: string, code: ErrorCodeValue, details: string): void {
        this.send(socket, { message_id: messageId, error_code: code, details });
    }

    private send(socket: WebSocket, frame: unknown): void {
        if (socket.readyState === socket.OPEN) {
            socket.send(JSON.stringify(frame));
        }
    }
}

function describeError(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}
