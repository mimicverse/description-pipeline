// Every semantic category renders with bounded examples and a complete-certificate link.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const app = readFileSync(new URL("../../src/description_pipeline/orchestration/static/app.js", import.meta.url), "utf8");
function functionSource(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  const end = app.indexOf("\nfunction ", start + 1);
  return app.slice(start, end < 0 ? undefined : end);
}
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ""; }
  append(...items) { this.children.push(...items); }
}
const certificateUrl = "/api/runs/actual/artifacts/verification-certificate.json";
const context = {
  document: { createElement: (tag) => new Element(tag) },
  state: { lastRun: { verification_reports: [{ path: "verification-certificate.json", url: certificateUrl }] } },
  expandable(parent, label) {
    const details = new Element("details");
    details.label = label;
    parent.append(details);
    return details;
  },
};
vm.createContext(context);
vm.runInContext(`const WARNING_ROW_LIMIT = 8;\n${functionSource("appendWarningSourceRows")}\n${functionSource("stageWarningRow")}\n${functionSource("renderStageWarnings")}`, context);
const warnings = Array.from({ length: 12 }, (_, index) => ({
  code: `warning.category_${index}`, label: `Category ${index}`, message: "Located source warning",
  warning_summary: true, records_total: 40, records_omitted: 32,
  source_rows_total: 40, source_rows_omitted: 32,
  objects: Array.from({ length: 8 }, (_, part) => ({
    object: `part-${part}`, field: "mass", source: "native", original: 0, actual: 0,
    hardware_qualified: false, native_observation_errors: [{ reason: "unreadable" }],
    missing_references: { items: [{ path: "missing.SLDPRT" }] },
  })),
}));
const root = new Element("root");
context.renderStageWarnings(root, { id: "verify", warnings, counts: { warnings: 480 } }, "Complete records stay in the certificate.");
const group = root.children[0];
assert.match(group.children[0].textContent, /480 records in 12 categories/);
const categories = group.children.filter((node) => node.tag === "details");
assert.equal(categories.length, 12);
assert.match(categories.at(-1).label, /warning.category_11/);
for (const category of categories) {
  const item = category.children[0];
  const sources = item.children.find((node) => node.tag === "details");
  const exampleBlock = sources.children.find((node) => node.tag === "pre");
  assert.deepEqual(JSON.parse(exampleBlock.textContent), warnings[0].objects);
  assert.match(sources.children.at(-1).textContent, /32 records and 32 source rows are not expanded/);
}
assert.equal(group.children.at(-1).href, certificateUrl);
assert.match(app, /preview\.warning_categories_total > diagnostic\.warnings\.length/);
console.log("Warning summary rendering preserves every category, bounded sources, counts and certificate access.");
