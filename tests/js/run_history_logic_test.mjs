// Focused regression for the pure run-history list helpers (no DOM).
// Run with: node tests/js/run_history_logic_test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  path.join(here, "..", "..", "src", "description_pipeline", "orchestration", "static", "run_history.js"),
  "utf8",
);
// Load the browser module as real ESM without a package.json in the way.
const { menuAction, overlayRun, resolveDeleted, resolveTitle } = await import(
  `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`
);

// resolveDeleted: the authoritative detail statement wins; a stale cached row cannot pin it.
assert.equal(resolveDeleted({ deleted: false }, { deleted: true }), false);
assert.equal(resolveDeleted({ deleted: true }, { deleted: false }), true);
assert.equal(resolveDeleted({}, { deleted: true }), true);
assert.equal(resolveDeleted({}, null), false);

// resolveTitle: the detail payload decides (null clears to the folder default); only a missing
// field falls back to the cached row.
assert.equal(resolveTitle({ title: "总装A" }, { title: "旧名" }), "总装A");
assert.equal(resolveTitle({ title: null }, { title: "旧名" }), "");
assert.equal(resolveTitle({}, { title: "曾用名" }), "曾用名");
assert.equal(resolveTitle({}, { title: null }), "");

// menuAction: an open menu survives same-run polls; a run change rebuilds; lost permission clears.
assert.equal(menuAction("run-1", "run-1", true), "keep");
assert.equal(menuAction("run-1", "run-2", true), "rebuild");
assert.equal(menuAction(null, "run-1", true), "rebuild");
assert.equal(menuAction("run-1", "run-1", false), "clear");

// overlayRun: the cached row updates in place (paging survives), a detail-only run still merges,
// and a defensive null run never throws.
const cached = { dag_run_id: "run-3", title: "旧名", deleted: false };
const runs = [cached];
overlayRun(runs, cached, { title: "新名" });
assert.equal(cached.title, "新名");
const detailOnly = { dag_run_id: "run-4", deleted: false };
overlayRun(runs, detailOnly, { deleted: true, deleted_at: "2026-10-09T21:00:00+08:00", deleted_by: "Andy" });
assert.deepEqual(detailOnly, {
  dag_run_id: "run-4",
  deleted: true,
  deleted_at: "2026-10-09T21:00:00+08:00",
  deleted_by: "Andy",
});
assert.equal(runs.length, 1);
overlayRun(runs, null, { title: "x" });
console.log("run_history_logic_test OK");
