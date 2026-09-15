import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';
import ts from 'typescript';

// Read the exact shipped pure functions without starting Studio's DOM/network loop.
const names = new Set(['parseEvaluationReport', 'evaluationResult', 'mechanicalEvaluation', 'cachedSchema', 'mechanicalEvidence', 'mechanicalPassed', 'recordedExecutionPassed', 'mechanicalResult',
  'recordedJudgeScore', 'judgeEvaluation', 'scoredEvaluation', 'actionEvaluations', 'overallEvaluation', 'evaluationNodeStatus', 'evaluationCoverage', 'evaluationHeadline']);
const source = ts.createSourceFile('app.js', fs.readFileSync(new URL('./app.js', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const functions = source.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name.text));
assert.equal(functions.length, names.size);
const context = vm.createContext({});
vm.runInContext(functions.map(node => node.getText(source)).join('\n'), context);
const node = { id: 'draft', gate: 'assert-output', schema: { title: 'string' }, judge: { score: 8.5 } };

function judged(score) { return { status: 'passed', judge_attempts: [{ judge: JSON.stringify({ score }) }] }; }

test('execution success cannot invent unconfigured evaluations', () => {
  const row = context.actionEvaluations({ id: 'plain' }, { status: 'passed' });
  assert.ok(row.checks.every(check => check.status === 'not_configured'));
  assert.equal(context.overallEvaluation({ has_qa: false }, { qa: '{"verdict":"pass"}' }).status, 'not_configured');
});

test('mechanical gates and schema are separately reported', () => {
  const detail = { status: 'failed', failure_kind: 'schema_failed', failure: 'Missing title' };
  assert.equal(context.mechanicalEvaluation(node, detail, 'gate').status, 'passed');
  assert.equal(context.mechanicalEvaluation(node, detail, 'schema').status, 'failed');
});

test('gate failure cannot imply the downstream schema ran', () => {
  const detail = { status: 'failed', failure_kind: 'gate_failed' };
  assert.equal(context.mechanicalEvaluation(node, detail, 'gate').status, 'failed');
  assert.equal(context.mechanicalEvaluation(node, detail, 'schema').status, 'not_evaluated');
});

test('cached model output has no fabricated fresh judge result', () => {
  assert.equal(context.judgeEvaluation(node, { ...judged(10), cached: true }).status, 'not_evaluated');
  assert.equal(context.mechanicalEvaluation(node, { status: 'cached', cached: true }, 'gate').status, 'passed');
});

test('zero score is a recorded failure, not missing evidence', () => {
  assert.equal(context.judgeEvaluation(node, judged(0)).status, 'failed');
  assert.equal(context.judgeEvaluation(node, judged(8.5)).status, 'passed');
});

test('malformed final report does not reuse an earlier passing score', () => {
  const detail = judged(10); detail.judge_attempts.push({ judge: '{broken' });
  assert.equal(context.judgeEvaluation(node, detail).status, 'not_evaluated');
});

test('kept best output still fails its semantic threshold when below target', () => {
  const detail = judged(8); detail.judge_attempts.push({ judge: '{"score":4}' });
  const result = context.judgeEvaluation({ ...node, judge: { score: 8.5, keep_best: true } }, detail);
  assert.equal(result.status, 'failed'); assert.equal(result.label, '8 / target 8.5');
});

test('QA needs an exact recorded verdict; quoted pass text is not a pass', () => {
  assert.equal(context.overallEvaluation({ has_qa: true }, { qa: '{"verdict":"fail","issues":["pass is unsupported"]}' }).status, 'failed');
  assert.equal(context.overallEvaluation({ has_qa: true }, { qa: '{broken' }).status, 'not_evaluated');
  assert.equal(context.overallEvaluation({ has_qa: true }, { qa: '```json\n{"verdict":"pass"}\n```' }).status, 'passed');
});

test('conditional skips are explicit and excluded from applicable coverage', () => {
  const rows = [context.actionEvaluations(node, { status: 'skipped' })];
  const result = context.evaluationCoverage(rows);
  assert.equal(result.configured, 0); assert.equal(result.skipped, 3); assert.equal(result.passed, 0);
});

test('pending or failed execution does not count as a completed check', () => {
  const row = context.actionEvaluations(node, { status: 'failed', failure_kind: 'model_error' });
  assert.equal(context.evaluationCoverage([row]).passed, 0);
});


test('cache hits do not claim a fresh schema check', () => {
  assert.equal(context.mechanicalEvaluation(node, { status: 'cached', cached: true }, 'schema').status, 'not_evaluated');
});

test('QA graph state follows the exact verdict, not a pass substring in issues', () => {
  const detail = { qa: '{"verdict":"fail","issues":["pass is unsupported"]}' };
  assert.equal(context.evaluationNodeStatus({ kind: 'qa', status: 'passed' }, { has_qa: true }, detail), 'failed');
});


test('zero applicable checks cannot read as a passing evaluation', () => {
  assert.equal(context.evaluationHeadline([], { configured: 0 }), '0 actions · no applicable checks');
});

test('legacy output presence cannot prove a gate or schema passed', () => {
  for (const kind of ['gate', 'schema']) {
    assert.equal(context.mechanicalEvaluation(node, { status: 'passed' }, kind).status, 'not_evaluated');
    assert.equal(context.mechanicalEvaluation(node, { status: 'passed', attempts: 1 }, kind).status, 'passed');
  }
});
