import { mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

import { StringEnum } from "@earendil-works/pi-ai";
import {
  formatSize,
  truncateHead,
  withFileMutationQueue,
  type ExtensionAPI,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const ACTIONS = ["doctor", "schema", "actions", "add", "list", "create", "graph", "path", "validate", "run", "batch", "batch-status", "batch-cancel", "runs", "detail", "compare", "show", "set", "stats", "eval", "reports", "schedule", "automations", "automation"] as const;
const PIW = fileURLToPath(new URL("../bin/piw", import.meta.url));
const TOOL_MAX_LINES = 500;
const TOOL_MAX_BYTES = 24 * 1024;

async function boundedOutput(output: string) {
  const truncation = truncateHead(output, { maxLines: TOOL_MAX_LINES, maxBytes: TOOL_MAX_BYTES });
  if (!truncation.truncated) return { text: truncation.content, truncation };

  const directory = await mkdtemp(join(tmpdir(), "pi-workflows-output-"));
  const fullOutputPath = join(directory, "output.txt");
  await withFileMutationQueue(fullOutputPath, () => writeFile(fullOutputPath, output, "utf8"));
  const omittedLines = truncation.totalLines - truncation.outputLines;
  const omittedBytes = truncation.totalBytes - truncation.outputBytes;
  return {
    text: `${truncation.content}\n\n[Output truncated: showing ${truncation.outputLines} of ${truncation.totalLines} lines (${formatSize(truncation.outputBytes)} of ${formatSize(truncation.totalBytes)}); ${omittedLines} lines (${formatSize(omittedBytes)}) omitted. Full output: ${fullOutputPath}]`,
    truncation,
    fullOutputPath,
  };
}

export function argumentsFor(params: Record<string, unknown>): string[] {
  const action = String(params.action ?? "list");
  if (!(ACTIONS as readonly string[]).includes(action)) throw new Error(`unsupported Pi Workflows action: ${action}`);
  const command = action === "list" ? "ls" : action;
  const args = [command];
  if (!["ls", "doctor", "schema", "actions", "create", "batch-status", "batch-cancel", "automations", "automation"].includes(command)) {
    const workflow = typeof params.workflow === "string" ? params.workflow.trim() : "";
    if (!workflow) throw new Error(`${action} requires a workflow id or unique name`);
    args.push(workflow);
  }
  if (command === "create") {
    const name = typeof params.name === "string" ? params.name.trim() : "";
    if (!name) throw new Error("create requires a workflow name");
    args.push(name);
    if (typeof params.directory === "string" && params.directory.trim()) args.push("--dir", params.directory.trim());
    if (typeof params.model === "string" && params.model.trim()) args.push("--model", params.model.trim());
    if (typeof params.qaModel === "string" && params.qaModel.trim()) args.push("--qa-model", params.qaModel.trim());
    if (typeof params.thinking === "string" && params.thinking.trim()) args.push("--thinking", params.thinking.trim());
    if (params.workers !== undefined) args.push("--workers", String(params.workers));
    if (typeof params.actionId === "string" && params.actionId.trim()) args.push("--action", params.actionId.trim());
  }
  if (command === "actions") {
    if (typeof params.actionId === "string" && params.actionId.trim()) args.push(params.actionId.trim());
  }
  if (command === "add") {
    const actionId = typeof params.actionId === "string" ? params.actionId.trim() : "";
    if (!actionId) throw new Error("add requires an actionId from the reusable action catalog");
    args.push(actionId);
    if (typeof params.id === "string" && params.id.trim()) args.push("--id", params.id.trim());
    if (typeof params.needs === "string" && params.needs.trim()) args.push("--needs", params.needs.trim());
  }
  if (command === "show") {
    const step = typeof params.step === "string" ? params.step.trim() : "";
    if (!step) throw new Error("show requires a step id");
    args.push(step);
    if (params.resolved === true) args.push("--resolved");
  }
  if (command === "detail") {
    if (typeof params.run === "string" && params.run.trim()) args.push(params.run.trim());
    if (typeof params.step === "string" && params.step.trim()) args.push("--step", params.step.trim());
    if (params.io === true) args.push("--io");
    if (params.all === true) args.push("--all");
  }
  if (command === "compare") {
    const baseline = typeof params.baselineRun === "string" ? params.baselineRun.trim() : "";
    const candidate = typeof params.candidateRun === "string" ? params.candidateRun.trim() : "";
    if (!baseline || !candidate) throw new Error("compare requires baselineRun and candidateRun");
    args.push(baseline, candidate);
    if (typeof params.step === "string" && params.step.trim()) args.push("--step", params.step.trim());
  }
  if (command === "set") {
    const step = typeof params.step === "string" ? params.step.trim() : "";
    if (!step) throw new Error("set requires a step id");
    args.push(step);
    if (typeof params.model === "string") args.push("--model", params.model);
    if (typeof params.thinking === "string") args.push("--thinking", params.thinking);
    if (typeof params.promptFile === "string" && params.promptFile.trim()) args.push("--prompt-file", params.promptFile.trim());
    if (typeof params.judgeModel === "string") args.push("--judge-model", params.judgeModel);
    if (typeof params.judgeThinking === "string") args.push("--judge-thinking", params.judgeThinking);
    if (params.judgeScore !== undefined) args.push("--judge-score", String(params.judgeScore));
    if (params.judgeMaxIters !== undefined) args.push("--judge-max-iters", String(params.judgeMaxIters));
    if (typeof params.judgePromptFile === "string" && params.judgePromptFile.trim()) args.push("--judge-prompt-file", params.judgePromptFile.trim());
    if (params.judgeKeepBest === true) args.push("--judge-keep-best");
    if (params.judgeKeepBest === false) args.push("--no-judge-keep-best");
    if (params.clearJudge === true) args.push("--clear-judge");
  }
  if (command === "run") {
    if (typeof params.node === "string" && params.node.trim()) args.push("--node", params.node.trim());
    const input = typeof params.input === "string" ? params.input : undefined;
    const inputFile = typeof params.inputFile === "string" ? params.inputFile.trim() : "";
    if (input !== undefined && inputFile) throw new Error("run accepts input or inputFile, not both");
    if (input !== undefined) args.push("--input", input);
    if (inputFile) args.push("--input-file", inputFile);
    if (params.noCache === true) args.push("--no-cache");
  }
  if (command === "batch") {
    const inputs = typeof params.inputs === "string" ? params.inputs.trim() : "";
    if (!inputs) throw new Error("batch requires an inputs corpus path");
    args.push("--inputs", inputs);
    if (typeof params.inputFile === "string" && params.inputFile.trim()) args.push("--input-file", params.inputFile.trim());
    if (params.parallel !== undefined) args.push("--parallel", String(params.parallel));
    if (params.limit !== undefined) args.push("--limit", String(params.limit));
    const out = typeof params.out === "string" ? params.out.trim() : "";
    const resume = typeof params.resume === "string" ? params.resume.trim() : "";
    if (out && resume) throw new Error("batch accepts out or resume, not both");
    if (out) args.push("--out", out);
    if (resume) args.push("--resume", resume);
    if (params.requireAll === true) args.push("--require-all");
    if (params.stopAfterFailures !== undefined) args.push("--stop-after-failures", String(params.stopAfterFailures));
    if (params.maxTokens !== undefined) args.push("--max-tokens", String(params.maxTokens));
    if (params.maxCost !== undefined) args.push("--max-cost", String(params.maxCost));
    if (typeof params.outputStep === "string" && params.outputStep.trim()) args.push("--output-step", params.outputStep.trim());
    if (params.itemTimeoutSeconds !== undefined) args.push("--item-timeout", String(params.itemTimeoutSeconds));
    if (params.gitHistory === true) args.push("--git-history");
    if (params.detach === true) args.push("--detach");
  }
  if (command === "eval") {
    const inputs = typeof params.inputs === "string" ? params.inputs.trim() : "";
    const inputFile = typeof params.inputFile === "string" ? params.inputFile.trim() : "";
    const models = typeof params.models === "string" ? params.models.trim() : "";
    if (!inputs || !inputFile || !models) throw new Error("eval requires inputs, inputFile, and models");
    args.push("--inputs", inputs, "--input-file", inputFile, "--models", models);
    if (params.parallel !== undefined) args.push("--parallel", String(params.parallel));
    if (params.limit !== undefined) args.push("--limit", String(params.limit));
  }
  if (command === "reports" && params.showReports === true) args.push("--show");
  if (command === "batch-status" || command === "batch-cancel") {
    const batchDirectory = typeof params.batchDirectory === "string" ? params.batchDirectory.trim() : "";
    if (!batchDirectory) throw new Error(`${command} requires a batchDirectory`);
    args.push(batchDirectory);
  }
  if (command === "schedule") {
    const interval = params.intervalMinutes === undefined ? undefined : Number(params.intervalMinutes);
    const daily = typeof params.daily === "string" ? params.daily.trim() : "";
    if ((interval === undefined) === !daily) throw new Error("schedule requires exactly one of intervalMinutes or daily");
    if (interval !== undefined) args.push("--interval-minutes", String(interval));
    else args.push("--daily", daily);
    if (typeof params.id === "string" && params.id.trim()) args.push("--id", params.id.trim());
    if (typeof params.name === "string" && params.name.trim()) args.push("--name", params.name.trim());
    if (params.timeoutSeconds !== undefined) args.push("--timeout", String(params.timeoutSeconds));
    if (params.stopAfter !== undefined) args.push("--stop-after", String(params.stopAfter));
  }
  if (command === "automation") {
    const automationAction = typeof params.automationAction === "string" ? params.automationAction.trim() : "";
    const id = typeof params.id === "string" ? params.id.trim() : "";
    if (!automationAction || !id) throw new Error("automation requires automationAction and id");
    args.push(automationAction, id);
  }
  if (params.json === true) args.push("--json");
  return args;
}

export default function piWorkflows(pi: ExtensionAPI) {
  pi.registerTool({
    name: "pi_workflows",
    label: "Pi Workflows",
    description: `Create, run, inspect, QA, compare, evaluate, and optimize deterministic workflow graphs. Inspect whole runs or individual nodes, configure models and judges, compare cost/tokens/latency, and execute one graph over large corpora. Output is limited to ${TOOL_MAX_LINES} lines or ${formatSize(TOOL_MAX_BYTES)}; complete truncated output is saved to a temporary file.`,
    promptSnippet: "Run deterministic workflows; inspect, QA, compare, and optimize every node",
    promptGuidelines: [
      "Use pi_workflows validate before pi_workflows run; validation is free and failed validation must block paid execution.",
      "Use the actions catalog before authoring common extraction, review, research, coding, JSONL, or exact-item patterns; add expands templates into ordinary inspectable nodes.",
      "After every run, use detail for the whole trace or one step; use compare before promoting a model, prompt, reasoning, or judge change.",
      "Use set to configure a node and its independent judge, run with node to force it fresh, and eval to compare models over a fixed corpus.",
      "For many inputs, canary with batch limit first, then use batch with detach and requireAll; poll batch-status until every item has a complete execution contract.",
      "Use pi_workflows schedule only after validation and one successful manual smoke; scheduling validates again and fails closed.",
      "Use pi_workflows only when a repeatable graph earns its complexity; use ordinary tools for a one-step task.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      workflow: Type.Optional(Type.String({ maxLength: 200 })),
      name: Type.Optional(Type.String({ maxLength: 200 })),
      directory: Type.Optional(Type.String({ maxLength: 2_000 })),
      model: Type.Optional(Type.String({ maxLength: 200 })),
      qaModel: Type.Optional(Type.String({ maxLength: 200 })),
      thinking: Type.Optional(StringEnum(["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const)),
      workers: Type.Optional(Type.Integer({ minimum: 1, maximum: 16 })),
      actionId: Type.Optional(Type.String({ maxLength: 100 })),
      needs: Type.Optional(Type.String({ maxLength: 2_000 })),
      step: Type.Optional(Type.String({ maxLength: 200 })),
      run: Type.Optional(Type.String({ maxLength: 200 })),
      baselineRun: Type.Optional(Type.String({ maxLength: 200 })),
      candidateRun: Type.Optional(Type.String({ maxLength: 200 })),
      io: Type.Optional(Type.Boolean()),
      all: Type.Optional(Type.Boolean()),
      node: Type.Optional(Type.String({ maxLength: 200 })),
      promptFile: Type.Optional(Type.String({ maxLength: 2_000 })),
      judgeModel: Type.Optional(Type.String({ maxLength: 200 })),
      judgeThinking: Type.Optional(StringEnum(["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const)),
      judgeScore: Type.Optional(Type.Number({ minimum: 0 })),
      judgeMaxIters: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
      judgePromptFile: Type.Optional(Type.String({ maxLength: 2_000 })),
      judgeKeepBest: Type.Optional(Type.Boolean()),
      clearJudge: Type.Optional(Type.Boolean()),
      input: Type.Optional(Type.String({ maxLength: 100_000 })),
      inputFile: Type.Optional(Type.String({ maxLength: 2_000 })),
      inputs: Type.Optional(Type.String({ maxLength: 2_000 })),
      parallel: Type.Optional(Type.Integer({ minimum: 1, maximum: 32 })),
      limit: Type.Optional(Type.Integer({ minimum: 1 })),
      out: Type.Optional(Type.String({ maxLength: 2_000 })),
      resume: Type.Optional(Type.String({ maxLength: 2_000 })),
      requireAll: Type.Optional(Type.Boolean()),
      stopAfterFailures: Type.Optional(Type.Integer({ minimum: 1 })),
      maxTokens: Type.Optional(Type.Integer({ minimum: 1 })),
      maxCost: Type.Optional(Type.Number({ exclusiveMinimum: 0 })),
      outputStep: Type.Optional(Type.String({ maxLength: 200 })),
      itemTimeoutSeconds: Type.Optional(Type.Integer({ minimum: 1, maximum: 86_400 })),
      gitHistory: Type.Optional(Type.Boolean()),
      detach: Type.Optional(Type.Boolean()),
      batchDirectory: Type.Optional(Type.String({ maxLength: 2_000 })),
      noCache: Type.Optional(Type.Boolean()),
      models: Type.Optional(Type.String({ maxLength: 2_000 })),
      showReports: Type.Optional(Type.Boolean()),
      resolved: Type.Optional(Type.Boolean()),
      intervalMinutes: Type.Optional(Type.Integer({ minimum: 1 })),
      daily: Type.Optional(Type.String({ maxLength: 5 })),
      timeoutSeconds: Type.Optional(Type.Integer({ minimum: 1, maximum: 86_400 })),
      stopAfter: Type.Optional(Type.Integer({ minimum: 1 })),
      id: Type.Optional(Type.String({ maxLength: 100 })),
      automationAction: Type.Optional(StringEnum(["show", "pause", "resume", "run", "delete"] as const)),
      json: Type.Optional(Type.Boolean()),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      const args = argumentsFor(params as Record<string, unknown>);
      onUpdate?.({
        content: [{ type: "text", text: `piw ${args.slice(0, 2).join(" ")}…` }],
        details: { command: "piw", args, state: "running" },
      });
      const result = await pi.exec(PIW, args, { cwd: ctx.cwd, signal, timeout: 3_600_000 });
      const output = `${result.stdout || ""}${result.stderr ? `\n${result.stderr}` : ""}`.trim();
      if (result.code !== 0) throw new Error(output || `piw exited ${result.code}`);
      const bounded = await boundedOutput(output || "ok");
      return {
        content: [{ type: "text", text: bounded.text }],
        details: {
          command: PIW,
          args,
          code: result.code,
          truncated: bounded.truncation.truncated,
          truncation: bounded.truncation,
          fullOutputPath: bounded.fullOutputPath,
        },
      };
    },
  });
}
