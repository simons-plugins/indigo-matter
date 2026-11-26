# Storage written by bridge 0.17.5 on matter.js 0.17.8

Used by `restore.test.ts` ("a 0.17.x storage directory") to prove the 0.18 node boots on storage an
older release wrote: same endpoint numbers restored from `endpoint-map.json`, and a persisted
attribute (`currentLevel`) still read back. The tests boot on a COPY, never on this directory.

Contents: two accessories (`indigo-111` onOffLight "Fixture Lamp", `indigo-222` dimmableLight
"Fixture Dimmer" pushed to level 20% = currentLevel 51), uncommissioned (no fabric), identity passed in
by the test (`installId fixture000000001`, passcode 20202021, discriminator 3841).

Generated with `generate.mjs` against a `git clone --local --branch main` of this repo at e0a8d8f
(bridge-node 0.17.5, `npm ci`, `npm run build`):

    node generate.mjs <clone>/bridge-node <empty-output-dir>

Do not regenerate it with 0.18: the point is that an OLDER release wrote it.
