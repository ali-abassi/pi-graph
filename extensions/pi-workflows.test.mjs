import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import piWorkflows, { argumentsFor } from "./pi-workflows.ts";


test("tool arguments preserve explicit workflow inputs and machine output", () => {
  assert.deepEqual(argumentsFor({ action: "list", json: true }), ["ls", "--json"]);
  assert.deepEqual(
    argumentsFor({ action: "run", workflow: "triage", node: "qa", input: "case", noCache: true, json: true }),
    ["run", "triage", "--node", "qa", "--input", "case", "--no-cache", "--json"],
  );
  assert.deepEqual(argumentsFor({ action: "doctor", json: true }), ["doctor", "--json"]);
  assert.deepEqual(argumentsFor({ action: "schema", json: true }), ["schema", "--json"]);
  assert.deepEqual(argumentsFor({ action: "create", name: "Release notes", workers: 2 }), ["create", "Release notes", "--workers", "2"]);
  assert.deepEqual(
    argumentsFor({ action: "batch", workflow: "enrich", inputs: "items.jsonl", parallel: 8, requireAll: true, detach: true, json: true }),
    ["batch", "enrich", "--inputs", "items.jsonl", "--parallel", "8", "--require-all", "--detach", "--json"],
  );
  assert.deepEqual(
    argumentsFor({ action: "batch-status", batchDirectory: "/tmp/batch", json: true }),
    ["batch-status", "/tmp/batch", "--json"],
  );
  assert.deepEqual(argumentsFor({ action: "schedule", workflow: "triage", daily: "09:00", stopAfter: 3 }), ["schedule", "triage", "--daily", "09:00", "--stop-after", "3"]);
  assert.deepEqual(argumentsFor({ action: "automation", automationAction: "resume", id: "piw-triage" }), ["automation", "resume", "piw-triage"]);
  assert.throws(() => argumentsFor({ action: "run" }), /requires a workflow/);
  assert.throws(() => argumentsFor({ action: "schedule", workflow: "triage" }), /exactly one/);
  assert.throws(() => argumentsFor({ action: "show", workflow: "triage" }), /requires a step/);
  assert.throws(() => argumentsFor({ action: "batch", workflow: "triage" }), /requires an inputs/);
});


test("Pi package registers a bounded native tool and throws on CLI failure", async () => {
  let tool;
  const pi = {
    registerTool(value) { tool = value; },
    async exec() { return { stdout: "invalid graph", stderr: "", code: 1, killed: false }; },
  };
  piWorkflows(pi);
  assert.equal(tool.name, "pi_workflows");
  await assert.rejects(
    tool.execute("id", { action: "validate", workflow: "broken" }, undefined, undefined, { cwd: "/tmp" }),
    /invalid graph/,
  );
});


test("native tool truncates large output and preserves the complete result", async () => {
  let tool;
  const output = "workflow row\n".repeat(600);
  const pi = {
    registerTool(value) { tool = value; },
    async exec() { return { stdout: output, stderr: "", code: 0, killed: false }; },
  };
  piWorkflows(pi);
  const result = await tool.execute("id", { action: "list" }, undefined, undefined, { cwd: "/tmp" });
  assert.equal(result.details.truncated, true);
  assert.match(result.content[0].text, /Output truncated/);
  assert.equal(await readFile(result.details.fullOutputPath, "utf8"), output.trim());
});
