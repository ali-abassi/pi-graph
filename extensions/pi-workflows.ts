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

const ACTIONS = ["doctor", "schema", "list", "create", "graph", "path", "validate", "run", "runs", "detail", "show", "stats", "schedule", "automations", "automation"] as const;
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
  if (!["ls", "doctor", "schema", "create", "automations", "automation"].includes(command)) {
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
  }
  if (command === "show") {
    const step = typeof params.step === "string" ? params.step.trim() : "";
    if (!step) throw new Error("show requires a step id");
    args.push(step);
    if (params.resolved === true) args.push("--resolved");
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
    description: `Create, validate, run, and inspect deterministic Pi workflow graphs. Use workflows for repeatable multi-step work whose ordering, gates, routes, evidence, or schedule must be mechanical rather than remembered by a model. Output is limited to ${TOOL_MAX_LINES} lines or ${formatSize(TOOL_MAX_BYTES)}; complete truncated output is saved to a temporary file.`,
    promptSnippet: "Operate deterministic workflow DAGs with explicit gates and evidence",
    promptGuidelines: [
      "Use pi_workflows validate before pi_workflows run; validation is free and failed validation must block paid execution.",
      "Use pi_workflows detail, show, and stats to verify artifacts, gates, cost, and cache behavior instead of inferring success from a process exit alone.",
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
      step: Type.Optional(Type.String({ maxLength: 200 })),
      node: Type.Optional(Type.String({ maxLength: 200 })),
      input: Type.Optional(Type.String({ maxLength: 100_000 })),
      inputFile: Type.Optional(Type.String({ maxLength: 2_000 })),
      noCache: Type.Optional(Type.Boolean()),
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
