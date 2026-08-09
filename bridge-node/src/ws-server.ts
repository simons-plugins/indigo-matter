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
    describeError,
    describeErrorWithStack,
    endpointMapInvalidDetails,
    ErrorCode,
    type ErrorCodeValue,
    EventName,
    type EventFrame,
    type EventNameValue,
    type HandshakeFrame,
    PROTOCOL_VERSION,
    ProtocolError,
    RECOVERY_COMMANDS,
    UNATTACHED_TIMEOUT_MS,
    WINDOW_DURATION_DEFAULT_SECONDS,
    WINDOW_DURATION_MAX_SECONDS,
    WINDOW_DURATION_MIN_SECONDS,
} from "./protocol.js";
import {
    parseDeviceId,
    parseEndpointSpec,
    parseEndpointSpecs,
    parseFabricIndex,
    parsePreserveEndpointNumbers,
    parseReplaceAll,
} from "./reconcile.js";

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
    /**
     * Tail of this socket's handler chain. §1 promises frames are processed in
     * receipt order, and handlers genuinely await (`open_commissioning_window`
     * does crypto), so each frame is queued behind the previous one rather than
     * dispatched concurrently.
     */
    pending: Promise<void>;
}

/**
 * A §3 command handler. Argument shape is validated here (that is what
 * `malformed_args`/`unknown_role` mean); everything that needs to know the live
 * endpoint set is decided behind {@link BridgeFacade}.
 */
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
        // §133: the settled variant, not the sync read — it bounded-retries
        // the transient "not initialized" window a last-fabric unpair leaves
        // behind, so this handler does not surface a spurious failure moments
        // after an unpair that already succeeded.
        this.#handlers.set("get_pairing", async () => this.options.bridge.getPairingSettled());
        this.#handlers.set("open_commissioning_window", async args => this.handleOpenWindow(args));
        this.#handlers.set("upsert_endpoint", async args =>
            this.options.bridge.upsertEndpoint(parseEndpointSpec(args.endpoint)),
        );
        this.#handlers.set("remove_endpoint", async args =>
            this.options.bridge.removeEndpoint(parseDeviceId(args.indigoDeviceId)),
        );
        this.#handlers.set("set_state", async args => this.handleSetState(args));
        this.#handlers.set("set_reachable", async args => this.handleSetReachable(args));
        // Returns the node's own outcome rather than a blanket `{}`: the plugin
        // reports this removal to the user in the past tense, and "there was no
        // fabric at that index" has to be able to reach it (§3.9).
        this.#handlers.set("remove_fabric", async args =>
            this.options.bridge.removeFabric(parseFabricIndex(args.fabricIndex)),
        );
        this.#handlers.set("factory_reset", async args => {
            await this.options.bridge.factoryReset(parsePreserveEndpointNumbers(args.preserveEndpointNumbers));
            return {};
        });
        this.#handlers.set("rebuild_endpoint_map", async () => this.options.bridge.rebuildEndpointMap());
        options.bridge.onWindowClosed(reason => this.sendEvent(EventName.windowClosed, { reason }));
        options.bridge.onCommand(data => this.sendEvent(EventName.command, data));
        options.bridge.onDriftDetected(drift => this.sendEvent(EventName.driftDetected, { drift }));
        options.bridge.onFabricsChanged((fabrics, change) =>
            this.sendEvent(EventName.fabricsChanged, { fabrics, change }),
        );
        options.bridge.onCommissioned(() => this.sendEvent(EventName.commissioned, {}));
        options.bridge.onDecommissioned(() => this.sendEvent(EventName.decommissioned, {}));
    }

    /**
     * Push an unsolicited event (§1/§5) to the attached client.
     *
     * Dropped when nobody is attached — the plugin re-`attach`es on reconnect
     * and gets a full reconcile, so a missed event has nothing to recover — but
     * never dropped *silently*: a run of these is how "I pressed the switch in
     * Apple Home and Indigo did nothing" looks from the node's side, and
     * without the line there is no way to tell it from a broken listener.
     */
    sendEvent(event: EventNameValue, data: Record<string, unknown>): void {
        const socket = this.#attached;
        if (socket === undefined) {
            const device = typeof data.indigoDeviceId === "number" ? ` for device ${data.indigoDeviceId}` : "";
            this.#log(`Dropping ${event} event${device}: no client is attached`);
            return;
        }
        const frame: EventFrame = { event, data };
        this.send(socket, frame);
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
            // `terminate`, not `close`: a graceful close waits for the peer's
            // close frame, and shutdown must not be held hostage by a plugin
            // that is itself mid-crash. `wss.close()` below then resolves.
            socket.terminate();
        }
        this.#clients.clear();
        this.#attached = undefined;
        if (wss !== undefined) {
            await new Promise<void>(resolve => wss.close(() => resolve()));
        }
    }

    private onConnection(socket: WebSocket): void {
        const state: ClientState = { attached: false, pending: Promise.resolve() };
        this.#clients.set(socket, state);

        // Before the handshake send: `ws` throws an unhandled 'error' event if a
        // socket fails with no listener attached, and a socket can fail on the
        // very first write.
        socket.on("error", error => this.#log(`Socket error: ${describeError(error)}`));

        // §2 step 1: the bare handshake frame, before anything else.
        const handshake: HandshakeFrame = {
            protocolVersion: PROTOCOL_VERSION,
            bridgeVersion: this.options.bridgeVersion,
            matterJsVersion: this.options.matterJsVersion,
        };
        socket.send(JSON.stringify(handshake));

        // §2: a connection that handshakes but never attaches is closed.
        const timeoutMs = this.options.unattachedTimeoutMs ?? UNATTACHED_TIMEOUT_MS;
        // Said once per socket, not once per period: while the node is refusing
        // the plugin holds this connection open indefinitely, and a line every
        // 10s for the life of the process buries everything else in the log.
        let announcedHold = false;
        const armUnattachedTimer = (): void => {
            state.unattachedTimer = setTimeout(() => {
                if (state.attached) {
                    return;
                }
                // Deferred, not disarmed, while we are refusing. In that state
                // the node itself is what stops this client attaching (§1.1
                // refuses `attach`), and the plugin deliberately HOLDS the
                // socket open un-attached because it is the only route to the
                // §3.11 rebuild. Closing it anyway put the two in a fight:
                // refuse, close, reconnect, refuse — every 10s, and a rebuild
                // the user had just confirmed could be cut off mid-flight by
                // our own timer. Re-arming rather than returning bare is what
                // keeps the check alive AFTER the rebuild clears the refusal:
                // a one-shot timer had already fired by then, so a client that
                // still never attached was never reaped again for the life of
                // the process.
                if (this.options.bridge.endpointMapRefusal() !== undefined) {
                    if (!announcedHold) {
                        announcedHold = true;
                        this.#log(
                            "Holding an un-attached connection open: the node is refusing to serve " +
                                "endpoints and this socket is the client's only route to " +
                                "rebuild_endpoint_map (§1.1)",
                        );
                    }
                    armUnattachedTimer();
                    return;
                }
                this.#log(`Closing connection that did not attach within ${timeoutMs}ms`);
                socket.close();
            }, timeoutMs);
            state.unattachedTimer.unref?.();
        };
        armUnattachedTimer();

        socket.on("message", (data, isBinary) => {
            if (isBinary) {
                // The protocol is JSON text frames only (§1); nothing to answer.
                this.#log("Dropping binary frame");
                return;
            }
            const raw = data.toString();
            // §1: strictly in receipt order — chain, never fan out. The tail
            // catch also guarantees the chain is never left rejected, so one bad
            // frame cannot stall every frame behind it.
            state.pending = state.pending
                .then(() => this.onMessage(socket, state, raw))
                .catch(error => this.#log(`Frame handling failed: ${describeErrorWithStack(error)}`));
        });
        socket.on("close", () => {
            clearTimeout(state.unattachedTimer);
            this.#clients.delete(socket);
            if (this.#attached === socket) {
                this.#attached = undefined;
                this.#log("Attached client disconnected");
            }
        });
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

        // The refuse-to-start gate comes before everything, including the
        // attach gate. In this state there is nothing to attach *to* — the node
        // will not create an endpoint until its map is rebuilt — so answering
        // `not_attached` would send the plugin round a reconnect loop that can
        // never succeed, and hide the one error that names the remedy. The
        // three §1.1 recovery commands are exempt from the attach requirement
        // for the same reason: the client holding this socket open never got to
        // attach, and `rebuild_endpoint_map` is the only way it ever will.
        const refusal = this.options.bridge.endpointMapRefusal();
        if (refusal !== undefined) {
            if (!RECOVERY_COMMANDS.has(command)) {
                this.sendError(
                    socket,
                    messageId,
                    ErrorCode.endpointMapInvalid,
                    endpointMapInvalidDetails(refusal),
                );
                return;
            }
        } else if (command !== "attach" && !state.attached) {
            // Gating: before `attach` the node has said nothing about which
            // commands it knows, so `not_attached` is the honest answer even for
            // a name it would otherwise reject (§1.1).
            this.sendError(socket, messageId, ErrorCode.notAttached, `${command} requires a successful attach first`);
            return;
        }

        const handler = this.#handlers.get(command);
        if (handler === undefined) {
            this.sendError(socket, messageId, ErrorCode.unknownCommand, `Unknown command ${command}`);
            return;
        }

        try {
            const result = await handler(commandArgs, socket, state);
            if (!this.send(socket, { message_id: messageId, result })) {
                // §1 promises exactly one response per request. A result we
                // could not put on the wire — a value that will not stringify,
                // a socket that failed mid-write — still owes the plugin an
                // answer, and `message_id` is still in scope to address it.
                this.sendError(socket, messageId, ErrorCode.internal, `Could not send the result of ${command}`);
            }
        } catch (error) {
            if (error instanceof ProtocolError) {
                this.sendError(socket, messageId, error.code, error.message);
                // §2: version skew fails closed — answer, then hang up.
                if (error.code === ErrorCode.versionMismatch) {
                    socket.close();
                }
                return;
            }
            // Stack node-side, message on the wire: `details` is what the Indigo
            // log shows a user, the stack is what we need to debug the node.
            this.#log(`Command ${command} failed: ${describeErrorWithStack(error)}`);
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

        // §3.1: parse before attaching state changes hands, so a malformed
        // endpoint set cannot supersede a healthy incumbent on its way to being
        // rejected.
        const endpoints = parseEndpointSpecs(args.endpoints);
        const replaceAll = parseReplaceAll(args.intent);

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
        this.#log(`Client attached (plugin ${pluginVersion}), reconciling ${endpoints.length} endpoint(s)`);

        // §3.1: a fresh connection is always a full reconcile. The mass-removal
        // guard lives behind the facade because only it knows the live set; a
        // refusal leaves this client attached but the endpoint set untouched,
        // which is what lets the plugin retry with an explicit intent.
        return this.options.bridge.reconcile(endpoints, replaceAll);
    }

    private async handleSetState(args: Record<string, unknown>): Promise<unknown> {
        const indigoDeviceId = parseDeviceId(args.indigoDeviceId);
        const states = args.states;
        if (typeof states !== "object" || states === null || Array.isArray(states)) {
            throw new ProtocolError(ErrorCode.malformedArgs, "states must be an object");
        }
        await this.options.bridge.setState(indigoDeviceId, states as Record<string, unknown>);
        return {};
    }

    private async handleSetReachable(args: Record<string, unknown>): Promise<unknown> {
        const indigoDeviceId = parseDeviceId(args.indigoDeviceId);
        if (typeof args.reachable !== "boolean") {
            throw new ProtocolError(ErrorCode.malformedArgs, "reachable must be a boolean");
        }
        await this.options.bridge.setReachable(indigoDeviceId, args.reachable);
        return {};
    }

    private async handleOpenWindow(args: Record<string, unknown>): Promise<unknown> {
        const duration = args.durationSeconds ?? WINDOW_DURATION_DEFAULT_SECONDS;
        if (
            typeof duration !== "number" ||
            !Number.isInteger(duration) ||
            duration < WINDOW_DURATION_MIN_SECONDS ||
            duration > WINDOW_DURATION_MAX_SECONDS
        ) {
            throw new ProtocolError(
                ErrorCode.malformedArgs,
                `durationSeconds must be an integer ${WINDOW_DURATION_MIN_SECONDS}-${WINDOW_DURATION_MAX_SECONDS}`,
            );
        }
        return this.options.bridge.openCommissioningWindow(duration);
    }

    private sendError(socket: WebSocket, messageId: string, code: ErrorCodeValue, details: string): void {
        this.send(socket, { message_id: messageId, error_code: code, details });
    }

    /**
     * Write one frame, reporting whether it went.
     *
     * Never throws: a serialisation failure here would otherwise escape into
     * whatever was mid-flight (an observable, a handler chain) rather than into
     * the response the caller is trying to send.
     */
    private send(socket: WebSocket, frame: unknown): boolean {
        if (socket.readyState !== socket.OPEN) {
            // Almost always the superseded incumbent: §2 closes it the moment a
            // new client attaches, and anything already queued for it lands in
            // that CLOSING window. Worth a line — it is also what a genuinely
            // wedged socket looks like.
            this.#log(`Dropping a frame for a socket in readyState ${socket.readyState} (not OPEN)`);
            return false;
        }
        try {
            socket.send(JSON.stringify(frame));
            return true;
        } catch (error) {
            this.#log(`Failed to send frame: ${describeErrorWithStack(error)}`);
            return false;
        }
    }
}
