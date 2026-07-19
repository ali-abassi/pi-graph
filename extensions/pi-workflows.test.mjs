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
  assert.deepEqual(argumentsFor({ action: "actions", actionId: "parallel-review", json: true }), ["actions", "parallel-review", "--json"]);
  assert.deepEqual(
    argumentsFor({ action: "add", workflow: "triage", actionId: "parallel-review", id: "review", needs: "draft" }),
    ["add", "triage", "parallel-review", "--id", "review", "--needs", "draft"],
  );
  assert.deepEqual(argumentsFor({ action: "create", name: "Release notes", workers: 2 }), ["create", "Release notes", "--workers", "2"]);
  assert.deepEqual(
    argumentsFor({ action: "create", name: "Review", actionId: "parallel-review" }),
    ["create", "Review", "--action", "parallel-review"],
  );
  assert.deepEqual(
    argumentsFor({ action: "batch", workflow: "enrich", inputs: "items.jsonl", parallel: 8, requireAll: true, maxTokens: 1000, maxCost: 2, outputStep: "publish", detach: true, json: true }),
    ["batch", "enrich", "--inputs", "items.jsonl", "--parallel", "8", "--require-all", "--max-tokens", "1000", "--max-cost", "2", "--output-step", "publish", "--detach", "--json"],
  );
  assert.deepEqual(
    argumentsFor({ action: "detail", workflow: "review", run: "run-2", step: "verdict", io: true, json: true }),
    ["detail", "review", "run-2", "--step", "verdict", "--io", "--json"],
  );
  assert.deepEqual(
    argumentsFor({ action: "compare", workflow: "review", baselineRun: "run-1", candidateRun: "run-2", step: "verdict", json: true }),
    ["compare", "review", "run-1", "run-2", "--step", "verdict", "--json"],
  );
  assert.deepEqual(
    argumentsFor({ action: "set", workflow: "review", step: "verdict", model: "test/luna", thinking: "low", judgeModel: "test/terra", judgeScore: 8, judgePromptFile: "qa.txt" }),
    ["set", "review", "verdict", "--model", "test/luna", "--thinking", "low", "--judge-model", "test/terra", "--judge-score", "8", "--judge-prompt-file", "qa.txt"],
  );
  assert.deepEqual(
    argumentsFor({ action: "eval", workflow: "review", inputs: "evals.jsonl", inputFile: "input.txt", models: "test/luna,test/terra", parallel: 2 }),
    ["eval", "review", "--inputs", "evals.jsonl", "--input-file", "input.txt", "--models", "test/luna,test/terra", "--parallel", "2"],
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
  assert.throws(() => argumentsFor({ action: "add", workflow: "triage" }), /requires an actionId/);
  assert.throws(() => argumentsFor({ action: "compare", workflow: "triage" }), /requires baselineRun/);
  assert.throws(() => argumentsFor({ action: "set", workflow: "triage" }), /requires a step/);
  assert.throws(() => argumentsFor({ action: "eval", workflow: "triage" }), /requires inputs/);
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
