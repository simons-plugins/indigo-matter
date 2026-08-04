/**
 * CLI argument parsing and defaults for the indigo-matter bridge node.
 *
 * Every knob the plugin needs to drive is a long-form `--flag value` argument;
 * launchd plists are easier to read that way than with environment variables.
 */

import { homedir } from "node:os";
import { join } from "node:path";

/** Default Matter operational/commissioning port (matter.js's own default). */
export const DEFAULT_MATTER_PORT = 5540;

/** Default loopback WebSocket port for the plugin protocol (controller uses 5580). */
export const DEFAULT_WS_PORT = 5581;

/** Storage root, sibling to the controller's directory. See PRD §4.3. */
export const DEFAULT_STORAGE_PATH = join(
    homedir(),
    "Library",
    "Application Support",
    "com.simons-plugins.indigo-matter",
    "bridge-node",
);

export interface BridgeConfig {
    /** Directory holding matter.js storage plus our own persisted install identity. */
    storagePath: string;
    /** Matter network port. */
    matterPort: number;
    /** Loopback port for the plugin protocol WebSocket server. */
    wsPort: number;
    /** Optional mDNS interface pin (maps to matter.js's `mdns.networkInterface`). */
    mdnsInterface?: string;
}

const USAGE = `indigo-matter-bridge

Usage: indigo-matter-bridge [options]

Options:
  --storage-path <dir>    Storage directory (default: ${DEFAULT_STORAGE_PATH})
  --matter-port <port>    Matter network port (default: ${DEFAULT_MATTER_PORT})
  --ws-port <port>        Loopback protocol WebSocket port (default: ${DEFAULT_WS_PORT})
  --mdns-interface <if>   Pin mDNS to a single network interface (default: all)
  --help                  Show this message
`;

function parsePort(name: string, raw: string): number {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 1 || value > 65535) {
        throw new Error(`${name} must be an integer 1-65535, got ${JSON.stringify(raw)}`);
    }
    return value;
}

/**
 * Parse `process.argv.slice(2)`. Throws on unknown or malformed arguments — a
 * bad launchd plist should fail loudly at start rather than silently run with
 * defaults on the wrong port.
 */
export function parseArgs(argv: readonly string[]): BridgeConfig | "help" {
    const config: BridgeConfig = {
        storagePath: DEFAULT_STORAGE_PATH,
        matterPort: DEFAULT_MATTER_PORT,
        wsPort: DEFAULT_WS_PORT,
    };

    for (let i = 0; i < argv.length; i++) {
        const flag = argv[i]!;
        if (flag === "--help" || flag === "-h") {
            return "help";
        }
        const value = argv[i + 1];
        if (value === undefined || value.startsWith("--")) {
            throw new Error(`${flag} requires a value`);
        }
        i++;
        switch (flag) {
            case "--storage-path":
                config.storagePath = value;
                break;
            case "--matter-port":
                config.matterPort = parsePort(flag, value);
                break;
            case "--ws-port":
                config.wsPort = parsePort(flag, value);
                break;
            case "--mdns-interface":
                config.mdnsInterface = value;
                break;
            default:
                throw new Error(`Unknown argument ${flag}`);
        }
    }

    return config;
}

export { USAGE };
