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
    ErrorCode,
    type FabricInfo,
    type PairingReport,
    ProtocolError,
    type StatusReport,
} from "./protocol.js";
import { type BridgeIdentity, nodeUniqueIdFor, serialNumberFor } from "./storage.js";

/** Test vendor id. Uncertified by design — see ADR-0006 "Attestation cuts both ways". */
export const VENDOR_ID = 0xfff1;
export const PRODUCT_ID = 0x8000;
export const VENDOR_NAME = "simons-plugins";
export const PRODUCT_NAME = "Indigo Matter Bridge";

/** The one hard-coded export of E0. E1 replaces this with protocol-driven CRUD. */
const E0_DEVICE_ID = 999001;
const E0_LABEL = "Indigo E0 Test";
const E0_ROLE = "onOffPlugInUnit";

/** PBKDF iteration count for enhanced-window verifiers. Spec floor is 1000. */
const PBKDF_ITERATIONS = 1000;
const PBKDF_SALT_BYTES = 32;

export { matterJsVersion };

/** `Endpoint.id` derivation — the identity key of BRIDGE_PROTOCOL §4.1/§6.3. */
export function endpointIdFor(indigoDeviceId: number): string {
    return `indigo-${indigoDeviceId}`;
}

/** Bridged Device Basic Information `UniqueID`, stable across restarts. */
export function uniqueIdFor(indigoDeviceId: number): string {
    return `indigo-${indigoDeviceId}`;
}

interface OpenWindow {
    expiresAt: Date;
    manualPairingCode: string;
    qrPairingCode: string;
    timer: NodeJS.Timeout;
}

export class BridgeNode implements BridgeFacade {
    #server?: ServerNode;
    #child?: Endpoint;
    #window?: OpenWindow;

    constructor(
        private readonly config: BridgeConfig,
        private readonly identity: BridgeIdentity,
        private readonly bridgeVersion: string,
        private readonly log: (message: string) => void = console.log,
    ) {}

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
        // matter.js resolves both of these lazily through its VariableService,
        // so setting them before `ServerNode.create` is what keeps storage out
        // of ~/.matter and pins mDNS when asked.
        environment.vars.set("storage.path", this.config.storagePath);
        if (this.config.mdnsInterface !== undefined) {
            environment.vars.set("mdns.networkInterface", this.config.mdnsInterface);
        }

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
            this.log(`Fabric ${action}: index ${fabricIndex} (${this.fabrics().length} total)`);
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

    getPairing(): PairingReport {
        const commissioned = this.server.lifecycle.isCommissioned;
        const window = this.#window;

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
        const server = this.server;
        const crypto = server.env.get(Crypto);

        const passcode = PaseClient.generateRandomPasscode(crypto);
        const discriminator = PaseClient.generateRandomDiscriminator(crypto);
        const salt = crypto.randomBytes(PBKDF_SALT_BYTES);
        const verifier = await PaseClient.generatePakePasscodeVerifier(crypto, passcode, {
            iterations: PBKDF_ITERATIONS,
            salt,
        });

        try {
            const paseServer = PaseServer.fromVerificationValue(server.env.get(SessionManager), verifier, {
                iterations: PBKDF_ITERATIONS,
                salt,
            });
            await server.env.get(DeviceCommissioner).allowEnhancedCommissioning(discriminator, paseServer, () => {
                this.clearWindow();
                this.log("Commissioning window closed");
            });
        } catch (error) {
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

        const expiresAt = this.setWindow(durationSeconds, manualPairingCode, qrPairingCode);
        return { manualPairingCode, qrPairingCode, windowExpiresAt: expiresAt.toISOString() };
    }

    private setWindow(durationSeconds: number, manualPairingCode: string, qrPairingCode: string): Date {
        this.clearWindow();
        const expiresAt = new Date(Date.now() + durationSeconds * 1000);
        // DeviceCommissioner does not time the window out for us — that timer
        // lived in the cluster behaviour we had to bypass — so we close it.
        const timer = setTimeout(() => {
            this.#window = undefined;
            this.log("Commissioning window expired");
            void this.#server?.env.get(DeviceCommissioner).endCommissioning();
        }, durationSeconds * 1000);
        timer.unref();
        this.#window = { expiresAt, manualPairingCode, qrPairingCode, timer };
        return expiresAt;
    }

    private clearWindow(): void {
        if (this.#window !== undefined) {
            clearTimeout(this.#window.timer);
            this.#window = undefined;
        }
    }

    async close(): Promise<void> {
        this.clearWindow();
        const server = this.#server;
        this.#server = undefined;
        this.#child = undefined;
        if (server !== undefined) {
            await server.close();
        }
    }
}

function describeError(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}
