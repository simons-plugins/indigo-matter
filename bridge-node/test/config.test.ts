/**
 * CLI argument parsing — a bad launchd plist must fail loudly, not run on the
 * wrong port with defaults.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DEFAULT_MATTER_PORT, DEFAULT_STORAGE_PATH, DEFAULT_WS_PORT, parseArgs } from "../src/config.js";

describe("parseArgs", () => {
    it("defaults to 5540 / 5581 and the Application Support storage path", () => {
        const config = parseArgs([]);
        assert.notEqual(config, "help");
        assert.deepEqual(config, {
            storagePath: DEFAULT_STORAGE_PATH,
            matterPort: DEFAULT_MATTER_PORT,
            wsPort: DEFAULT_WS_PORT,
        });
        assert.equal(DEFAULT_MATTER_PORT, 5540);
        assert.equal(DEFAULT_WS_PORT, 5581);
        assert.ok(DEFAULT_STORAGE_PATH.endsWith("com.simons-plugins.indigo-matter/bridge-node"));
        assert.ok(!DEFAULT_STORAGE_PATH.includes(".matter"));
    });

    it("reads every supported flag", () => {
        const config = parseArgs([
            "--storage-path",
            "/tmp/bridge",
            "--matter-port",
            "5541",
            "--ws-port",
            "5582",
            "--mdns-interface",
            "en0",
        ]);
        assert.deepEqual(config, {
            storagePath: "/tmp/bridge",
            matterPort: 5541,
            wsPort: 5582,
            mdnsInterface: "en0",
        });
    });

    it("leaves mdnsInterface unset when not given", () => {
        const config = parseArgs([]);
        assert.equal((config as { mdnsInterface?: string }).mdnsInterface, undefined);
    });

    it("rejects unknown flags, missing values and bad ports", () => {
        assert.throws(() => parseArgs(["--nope", "x"]), /Unknown argument --nope/);
        assert.throws(() => parseArgs(["--ws-port"]), /requires a value/);
        assert.throws(() => parseArgs(["--ws-port", "0"]), /must be an integer/);
        assert.throws(() => parseArgs(["--matter-port", "abc"]), /must be an integer/);
    });

    it("returns help for --help", () => {
        assert.equal(parseArgs(["--help"]), "help");
    });
});
