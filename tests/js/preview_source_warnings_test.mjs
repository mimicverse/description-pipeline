// Execute the production preview warning renderer with a minimal DOM boundary.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/description_pipeline/orchestration/static/app.js", import.meta.url), "utf8");
const start = source.indexOf("function renderPreviewDiagnostic(preview)");
const end = source.indexOf("async function loadPreview(", start);
assert.ok(start >= 0 && end > start);
const node = { hidden: true, textContent: "" };
const render = vm.runInNewContext(`${source.slice(start, end)}; renderPreviewDiagnostic`, {
  $: (id) => { assert.equal(id, "preview-diagnostic"); return node; },
});
render({ verified: true, warnings: [] });
assert.equal(node.hidden, true);
render({ verified: true, warnings: [{
  code: "limits.default_policy",
  message: "Engineering position range remains unqualified.",
  objects: [
    { object: "joint:left", field: "limits" },
    { object: "joint:right", field: "limits" },
  ],
}] });
assert.equal(node.hidden, false);
assert.match(node.textContent, /joint:left\.limits/);
assert.match(node.textContent, /joint:right\.limits/);
assert.match(node.textContent, /not native mechanical limits or hardware qualifications/);
assert.match(node.textContent, /unqualified/);
assert.doesNotMatch(node.textContent, /must not be published/);
render({ verified: false, failing_checks: [{ id: "binding", message: "mismatch" }], warnings: [] });
assert.match(node.textContent, /must not be published/);
assert.match(node.textContent, /binding/);
render({ verified: true, warnings: [{
  code: "limits.default_policy", message: "<script>unsafe</script>",
  detail: { objects: [{ object: "joint:third", field: "limits" }] },
}] });
assert.match(node.textContent, /joint:third\.limits/);
assert.match(node.textContent, /<script>unsafe<\/script>/);
console.log("preview_source_warnings_test OK");
