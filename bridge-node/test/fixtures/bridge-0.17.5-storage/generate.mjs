// Generates the bridge-0.17.5 / matter.js-0.17.8 storage fixture. Run with the clone's dist:
//   node gen-fixture.mjs <clone>/bridge-node <outDir>
import { pathToFileURL } from "node:url";
import { join } from "node:path";
const [, , root, out] = process.argv;
const { BridgeNode } = await import(pathToFileURL(join(root, "dist/node.js")).href);
const { Logger } = await import(pathToFileURL(join(root, "node_modules/@matter/main/dist/esm/export.js")).href).catch(() => ({}));
const bridge = new BridgeNode(
    { storagePath: out, matterPort: 0, wsPort: 0, mdnsInterface: process.env.MDNS_IF },
    { installId: "fixture000000001", passcode: 20202021, discriminator: 3841 },
    "0.17.5",
    m => {},
);
await bridge.start();
const spec = (id, role, label, states) => ({
    indigoDeviceId: id, role, label, reachable: true, states, options: {}, battery: false, publishedAs: `indigo-${id}`,
});
await bridge.reconcile([
    spec(111, "onOffLight", "Fixture Lamp", { onOff: false }),
    spec(222, "dimmableLight", "Fixture Dimmer", { onOff: true, level: 60 }),
], false);
await bridge.setState(222, { onOff: true, level: 20 });
const numbers = bridge.getStatus().endpoints.map(e => [e.indigoDeviceId, e.endpointNumber]);
console.log(JSON.stringify(numbers));
await bridge.close();
process.exit(0);
