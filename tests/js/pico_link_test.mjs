import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../../src/description_pipeline/orchestration/static/pico_link.js", import.meta.url), "utf8");
const { createLinkSetup } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const session = { session_id: "a".repeat(32), state: "ready", pairing_code: "fixture-only", runtime: { secret: "fixture-only" } };
const setup = createLinkSetup("https://portal.example.test:8443", session);
assert.deepEqual(setup, { schema_version: "solidworks-to-urdf.pico-link/v1", origin: "wss://portal.example.test:8443", session_id: session.session_id });
assert(!JSON.stringify(setup).includes("fixture-only"));
assert.equal(createLinkSetup("http://[::1]:8080", session).origin, "ws://[::1]:8080");
assert.throws(() => createLinkSetup("http://remote.example.test", session), /HTTPS/);
assert.throws(() => createLinkSetup("https://portal.example.test", { ...session, state: "unresolved" }), /active session/);
assert.throws(() => createLinkSetup("https://portal.example.test", { ...session, session_id: "../other" }), /active session/);
console.log("PICO link setup: active session binding and credential exclusion passed");
