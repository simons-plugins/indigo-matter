/**
 * The Matter side of the bridge: a ServerNode with an Aggregator at endpoint 1
 * and (in E0) a single hard-coded bridged child endpoint.
 *
 * All matter.js coupling lives here and in main.ts — ADR-0006's binding
 * constraint keeps it out of the Indigo plugin entirely, and this module keeps
 * it out of the protocol layer so the protocol is testable on its own.
 */

import { Endpoint, Environment, ServerNode, VendorId, version as matterJsVersion } from "@matter/main";
import { BridgedDeviceBasicInformationServer } from "@matter/main/behaviors/bridged-device-basic-information";
import { OnOffPlugInUnitDevice } from "@matter/main/devices/on-off-plug-in-unit";
import { AggregatorEndpoint } from "@matter/main/endpoints/aggregator";
import { Crypto } from "@matter/main";
import { DeviceCommissioner, PaseClient, PaseServer, SessionManager } from "@matter/main/protocol";
import { CommissioningFlowType, ManualPairingCodeCodec, QrPairingCodeCodec } from "@matter/main/types";

import type { BridgeConfig } from "./config.js";
import {
    type BridgeFacade,
    type CommissioningWindowResult,
    describeError,
    describeErrorWithStack,
    ErrorCode,
    type FabricInfo,
    type PairingReport,
    ProtocolError,
    Role,
    type StatusReport,
    type WindowClosedReason,
} from "./protocol.js";
import { type BridgeIdentity, nodeUniqueIdFor, serialNumberFor } from "./storage.js";
import { CommissioningWindow } from "./window.js";

/** Test vendor id. Uncertified by design — see ADR-0006 "Attestation cuts both ways". */
export const VENDOR_ID = 0xfff1;
export const PRODUCT_ID = 0x8000;
export const VENDOR_NAME = "simons-plugins";
export const PRODUCT_NAME = "Indigo Matter Bridge";

/** The one hard-coded export of E0. E1 replaces this with protocol-driven CRUD. */
const E0_DEVICE_ID = 999001;
const E0_LABEL = "Indigo E0 Test";
const E0_ROLE = Role.onOffPlugInUnit;

/** PBKDF iteration count for enhanced-window verifiers. Spec floor is 1000. */
const PBKDF_ITERATIONS = 1000;
const PBKDF_SALT_BYTES = 32;

export { matterJsVersion };

/** Bridged Device Basic Information `UniqueID`, stable across restarts. */
export function uniqueIdFor(indigoDeviceId: number): string {
    return `indigo-${indigoDeviceId}`;
}

/**
 * `Endpoint.id` derivation — the identity key of BRIDGE_PROTOCOL §4.1/§6.3.
 * Deliberately the *same* value as {@link uniqueIdFor}: one derivation means the
 * two can never drift apart, and §6.3's one-way identity flow reads directly.
 */
export const endpointIdFor = uniqueIdFor;

export class BridgeNode implements BridgeFacade {
    #server?: ServerNode;
    #child?: Endpoint;
    readonly #window: CommissioningWindow;

    constructor(
        private readonly config: BridgeConfig,
        private readonly identity: BridgeIdentity,
        private readonly bridgeVersion: string,
        private readonly log: (message: string) => void = console.log,
    ) {
        this.#window = new CommissioningWindow({
            log: message => this.log(message),
            onExpire: () => this.endMatterCommissioning(),
        });
    }

    /** Matter wants an integer software version; derive it from the npm semver. */
    private get softwareVersion(): number {
        const [major = 0, minor = 0, patch = 0] = this.bridgeVersion.split(".").map(part => Number.parseInt(part, 10));
        return (major || 0) * 10000 + (minor || 0) * 100 + (patch || 0);
    }

    /**
     * Build the node and bring it online. Aggregator is added first so it takes
     * endpoint 1; the bridged child then lands at 2.
     */
    async start(): Promise<void> {
        const environment = Environment.default;
        // matter.js resolves all of these lazily through its VariableService,
        // so setting them before `ServerNode.create` is what keeps storage out
        // of ~/.matter and pins mDNS when asked.
        environment.vars.set("storage.path", this.config.storagePath);
        if (this.config.mdnsInterface !== undefined) {
            environment.vars.set("mdns.networkInterface", this.config.mdnsInterface);
        }
        // We own SIGTERM/SIGINT outright. matter.js's ProcessManager installs its
        // own interrupt handlers when the runtime starts, and being registered
        // first they run first — tearing the ServerNode down concurrently with
        // main.ts's ordered shutdown. `runtime.signals` is the documented opt-out
        // (`ProcessManager.hasSignalSupport` reads it).
        environment.vars.set("runtime.signals", false);

        const server = await ServerNode.create({
            id: "indigo-matter-bridge",
            environment,
            network: { port: this.config.matterPort },
            commissioning: {
                passcode: this.identity.passcode,
                discriminator: this.identity.discriminator,
            },
            productDescription: {
                name: PRODUCT_NAME,
                deviceType: AggregatorEndpoint.deviceType,
            },
            basicInformation: {
                vendorName: VENDOR_NAME,
                vendorId: VendorId(VENDOR_ID),
                productName: PRODUCT_NAME,
                productId: PRODUCT_ID,
                serialNumber: serialNumberFor(this.identity),
                uniqueId: nodeUniqueIdFor(this.identity),
                hardwareVersion: 1,
                hardwareVersionString: "1",
                softwareVersion: this.softwareVersion,
                softwareVersionString: this.bridgeVersion,
            },
        });
        this.#server = server;

        const aggregator = new Endpoint(AggregatorEndpoint, { id: "aggregator" });
        await server.add(aggregator);

        const child = new Endpoint(OnOffPlugInUnitDevice.with(BridgedDeviceBasicInformationServer), {
            id: endpointIdFor(E0_DEVICE_ID),
            bridgedDeviceBasicInformation: {
                nodeLabel: E0_LABEL,
                productName: PRODUCT_NAME,
                productLabel: E0_LABEL,
                serialNumber: String(E0_DEVICE_ID),
                uniqueId: uniqueIdFor(E0_DEVICE_ID),
                reachable: true,
            },
        });
        await aggregator.add(child);
        this.#child = child;

        server.events.commissioning.fabricsChanged.on((fabricIndex, action) => {
            // A throw here propagates straight into matter.js's observable, and
            // `fabrics()` reads server state that is legitimately gone once
            // close() is under way. Log-only, never rethrow.
            try {
                this.log(`Fabric ${action}: index ${fabricIndex} (${this.fabrics().length} total)`);
            } catch (error) {
                try {
                    this.log(`Fabric ${action}: index ${fabricIndex} (count unavailable: ${describeError(error)})`);
                } catch {
                    // The logger itself failed; there is nowhere left to report.
                }
            }
        });

        await server.start();

        this.log(`Matter node online on port ${this.config.matterPort}, storage ${this.config.storagePath}`);
        this.logPairing();
    }

    /** Print the pairing codes the way an operator (and the plugin log) needs them. */
    logPairing(): void {
        const pairing = this.getPairing();
        if (pairing.commissioned && !pairing.windowOpen) {
            this.log(`Commissioned into ${pairing.fabrics.length} fabric(s); no commissioning window open`);
            return;
        }
        this.log(`Manual pairing code: ${pairing.manualPairingCode}`);
        this.log(`QR pairing code: ${pairing.qrPairingCode}`);
    }

    get server(): ServerNode {
        if (this.#server === undefined) {
            throw new ProtocolError(ErrorCode.internal, "Matter node not started");
        }
        return this.#server;
    }

    private fabrics(): FabricInfo[] {
        const fabrics = this.server.state.commissioning.fabrics;
        return Object.values(fabrics).map(fabric => ({
            fabricIndex: Number(fabric.fabricIndex),
            label: fabric.label,
            vendorId: Number(fabric.rootVendorId),
        }));
    }

    getStatus(): StatusReport {
        const child = this.#child;
        const endpoints =
            child === undefined
                ? []
                : [{ indigoDeviceId: E0_DEVICE_ID, endpointNumber: Number(child.number), role: E0_ROLE }];
        return {
            commissioned: this.server.lifecycle.isCommissioned,
            fabrics: this.fabrics(),
            endpointCount: endpoints.length,
            endpoints,
            // E6 introduces the persisted endpoint-number allocator; until then
            // there is no baseline to drift from.
            drift: [],
        };
    }

    /** §5: the sink for `window_closed`, wired up by the protocol server. */
    onWindowClosed(listener: (reason: WindowClosedReason) => void): void {
        this.#window.onClosed(listener);
    }

    getPairing(): PairingReport {
        const commissioned = this.server.lifecycle.isCommissioned;
        const window = this.#window.current;

        if (window !== undefined) {
            return {
                commissioned,
                windowOpen: true,
                windowExpiresAt: window.expiresAt.toISOString(),
                manualPairingCode: window.manualPairingCode,
                qrPairingCode: window.qrPairingCode,
                fabrics: this.fabrics(),
            };
        }

        if (!commissioned) {
            // The never-commissioned initial state: the basic window is open and
            // its codes are the persisted originals (§3.7).
            const codes = this.server.state.commissioning.pairingCodes;
            return {
                commissioned: false,
                windowOpen: true,
                windowExpiresAt: null,
                manualPairingCode: codes.manualPairingCode,
                qrPairingCode: codes.qrPairingCode,
                fabrics: this.fabrics(),
            };
        }

        return {
            commissioned: true,
            windowOpen: false,
            windowExpiresAt: null,
            manualPairingCode: null,
            qrPairingCode: null,
            fabrics: this.fabrics(),
        };
    }

    /**
     * Open an enhanced commissioning window so a further ecosystem can pair.
     * The passcode is ephemeral: matter.js holds only the PAKE verifier, so we
     * derive and keep the display codes ourselves for the window's lifetime.
     *
     * This drives `DeviceCommissioner` rather than the `AdministratorCommissioning`
     * cluster command, because in matter.js 0.17.8 that command asserts a remote
     * authenticated session (it records the requesting fabric in
     * `adminFabricIndex`) and therefore cannot be invoked from an offline agent.
     * The consequence is that the cluster's `windowStatus`/`adminFabricIndex`
     * attributes do not reflect a locally-opened window — a conformance gap for
     * E7 to close, not something E0's pairing flow depends on.
     */
    async openCommissioningWindow(durationSeconds: number): Promise<CommissioningWindowResult> {
        // Before anything Matter-side: `allowEnhancedCommissioning` swaps the PASE
        // commissioner and only then throws on a double-open, which would kill the
        // code the user is already holding. See CommissioningWindow.assertClosed.
        this.#window.assertClosed();

        const server = this.server;

        let discriminator: number;
        let passcode: number;
        try {
            const crypto = server.env.get(Crypto);
            passcode = PaseClient.generateRandomPasscode(crypto);
            discriminator = PaseClient.generateRandomDiscriminator(crypto);
            const salt = crypto.randomBytes(PBKDF_SALT_BYTES);
            const verifier = await PaseClient.generatePakePasscodeVerifier(crypto, passcode, {
                iterations: PBKDF_ITERATIONS,
                salt,
            });
            const paseServer = PaseServer.fromVerificationValue(server.env.get(SessionManager), verifier, {
                iterations: PBKDF_ITERATIONS,
                salt,
            });
            await server.env.get(DeviceCommissioner).allowEnhancedCommissioning(discriminator, paseServer, () => {
                this.#window.noteEnded();
            });
        } catch (error) {
            this.log(`Failed to open commissioning window: ${describeErrorWithStack(error)}`);
            throw new ProtocolError(ErrorCode.commissioningWindowFailed, describeError(error));
        }

        const manualPairingCode = ManualPairingCodeCodec.encode({ discriminator, passcode });
        const qrPairingCode = QrPairingCodeCodec.encode([
            {
                version: 0,
                vendorId: VENDOR_ID,
                productId: PRODUCT_ID,
                flowType: CommissioningFlowType.Standard,
                discriminator,
                passcode,
                discoveryCapabilities: 0b100, // on IP network
            },
        ]);

        // DeviceCommissioner *does* build a STANDARD_COMMISSIONING_TIMEOUT timer
        // in `#enterCommissioningMode`, but never calls `.start()` on it — a
        // matter.js 0.17.8 bug — so nothing on the Matter side would ever close
        // this window. Ours is the only timer. If upstream fixes that, the two
        // race, which is why we cap durationSeconds at Matter's own 900s maximum:
        // whichever fires first, the outcome is the same.
        const expiresAt = this.#window.open(durationSeconds, manualPairingCode, qrPairingCode);
        return { manualPairingCode, qrPairingCode, windowExpiresAt: expiresAt.toISOString() };
    }

    /** End the Matter-side window after ours expired. Never throws. */
    private endMatterCommissioning(): void {
        const server = this.#server;
        if (server === undefined) {
            return;
        }
        void server.env
            .get(DeviceCommissioner)
            .endCommissioning()
            .catch((error: unknown) => this.log(`Failed to end commissioning: ${describeError(error)}`));
    }

    async close(): Promise<void> {
        this.#window.clear();
        const server = this.#server;
        this.#server = undefined;
        this.#child = undefined;
        if (server !== undefined) {
            await server.close();
        }
    }
}
