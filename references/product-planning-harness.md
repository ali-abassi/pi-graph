# Product planning harness

Use this specialized read-only harness when the input is a project idea and the
required output is an implementation-ready product package before any product
code is written.

## What it produces

```text
idea
  -> normalized brief + mechanical validation
  -> controller-owned Exa competitor discovery + bounded first-party page capture
  -> source-linked competitive landscape and whitespace synthesis
  -> 6 parallel clean-context lanes + mechanical validation per lane
  -> product definition + judge/improve
  -> market frame, latent pains, switching case, compared pricing options + mechanical validation
  -> 3 competing design theses, selected design system + mechanical validation
  -> positioning, voice, identity roles, brand governance + mechanical validation
  -> 12-14 slide editable brand-deck specification
  -> sourced audience language vs inference + mechanical validation
  -> copy, claims, framework, stack, sitemap, and assets + mechanical validation
  -> parallel page plans and exact copy packs + mechanical validation
  -> cross-route consistency and copy-test plan + mechanical validation
  -> Cloudflare architecture + judge/improve
  -> implementation roadmap + mechanical validation
  -> cross-package consistency + mechanical validation
  -> compact semantic repair patch for every critical/high issue
  -> deterministic assembly of canonical approved-for-build copy
  -> final plan + judge/improve
  -> mechanical verification + integrity seal
```

The six lanes are product strategy, user experience, information architecture,
brand/visual direction, copy/conversion, and technical/operational planning.
They do not chat with one another. Each receives the same immutable brief in a
clean Pi context. The controller alone merges their outputs so parallelism does
not turn into consensus theater or shared-context drift.

## Competitor evidence and brand deck

`--research-mode auto` uses Exa when `EXA_API_KEY` is present and otherwise
finishes with an explicit unavailable boundary. `--research-mode exa` fails
closed when the key, discovery results, or at least two evidence-backed
competitors are unavailable. `fixture` is certification-only; `off` is an
explicit coverage loss. The controller runs two distinct discovery query
families, deduplicates domains, rejects social/review aggregators, and captures
at most 6 competitors, 3 pages per competitor, and 8,000 characters per page by
default. Exa content uses a 24-hour freshness window and bounded live-crawl
timeout. Exact page text and SHA-256 live under
`research/competitor-evidence.json`; the model never receives credentials and
never controls retrieval.

The competitive-landscape artifact separates observation from inference and
binds every competitor, observed price, repeated pattern, and whitespace claim
to those source IDs. Competitor pricing is category context, not proof of this
product's willingness to pay. The downstream brand deck has one narrative job
per slide and must cover audience, competitive whitespace, positioning,
personality, voice, logo direction, exact color tokens, typography, imagery,
product expression, and governance. Research-informed slides carry source IDs.
The renderer produces editable PowerPoint objects with `@oai/artifact-tool`;
the JSON artifact remains the canonical, mechanically verified source.

The design stage must compare at least three generative theses before selecting
one. It then fixes the visual, interaction, responsive, accessibility, and
signature systems plus an explicit cut list. The brand stage resolves
positioning, behavioral personality, voice, naming status, tagline role,
identity roles, and governance. A component library is never accepted as the
design direction.

The market stage chooses a buyer, user, incumbent, trigger, and switching case;
then records at least three latent pain points with evidence status, source
references, confidence, and observable falsification. It compares pricing
models and selects one concrete working option so implementation can proceed.
When willingness-to-pay or unit-economics evidence was not supplied or verified,
the selection is mechanically forced to `hypothesis-do-not-publish`. Smart
assumptions are allowed; fabricated market proof is not.

Every asset has a stable ID, exact route placements, production method, output
ratios/formats/variants, alt-text behavior, ownership or generation provenance,
failure fallback, and acceptance checks. Generated assets pin `gpt-image-2`,
require human review, and keep copy out of images unless exact text is rendered
and mechanically verified.

Every route appears exactly once in the sitemap with a parent, navigation label,
reader job, and indexing policy, and must have exactly one page plan and one copy pack. Page plans consume
the selected thesis, brand rules, framework constraints, and declared asset IDs;
then define sections, components, interactions, states, and typed copy slots.
They must cover the route channel's required reader jobs and declare every
route-level high-risk action as an interaction, so downstream copy is never
asked to repair a contradictory page contract.
Copy packs fill every slot exactly once with line-addressable candidate
copy, approved claim IDs, interaction IDs, state, limits, accessibility, and
localization rules. After cross-package consistency, the model returns only the
high-severity issue resolutions and copy lines that must change. The controller
then assembles every unchanged control, binds route-local copy IDs as
`route_id:copy_id`, and verifies the full canonical `copy-approval` artifact.
`approved-for-build` means truthful, coherent, contract-valid, and ready to
implement; performance remains `untested-candidates`.

The copy foundation separates supplied or observed audience language from
inference; records the earliest performance bottleneck; builds reader reality,
progress, value, mechanism, proof, objection, and action; and blocks copy-approved
claims without supplied or verified source evidence. Each route must include
three to five mechanism-separated candidates with one hypothesis, fixed
variables, guardrails, and falsification evidence. Paraphrase and action-
expectation checks precede conversion testing. LLM scores may select a candidate
for testing, but never declare a live winner.

## Quality semantics

The fast default target is `>= 9.0/10` on the raw weighted score for three
integration milestones: product definition, architecture, and final plan. The runtime—not the model—aggregates
criterion anchors. Every artifact must pass mechanical gates; lanes, pages,
copy packs, and other intermediate artifacts are not semantically scored under
the default `milestone` policy. Each milestone candidate revision replaces the
incumbent only when its valid score strictly improves; rejected attempts remain
in the ledger. The default is one revision and the configurable maximum is
three. After the configured revisions, a scored but below-target artifact does not block
the rest of planning: the controller selects the highest-scoring mechanically
valid candidate and labels its verdict `below_target_best_effort`. Mechanical
failure and an unusable or abstaining semantic verdict still fail closed.
Mechanical repair also uses a maximum of three attempts, but it cannot select an
invalid best effort. An attempt is kept only when it reduces the exact error
count; attempts and remaining errors are written to `structural-repairs/`.

The bundled planning and copy judges are intentionally `candidate`. The
copy-specific judge separates reader fit, message comprehension, truth,
motivation, action clarity, and voice/economy while deterministic gates remain
authoritative for claims, actions, states, variants, and truth/agency. Scores are
optimization and stop signals, not proof of preference or business performance.
Promote a judge only after independent labels, locked development/holdout sets,
adjacent-anchor boundary tests, bias probes, repeatability checks, and
predeclared thresholds. A high copy score is test-ready, never proven lift.

## Stack policy

The framework ADR compares Next.js, React Router framework mode, and Astro
against the actual routes, rendering needs, ecosystem, and Workers constraints.
The default preference is Next.js App Router on Cloudflare Workers through
OpenNext with shadcn/ui source, Tailwind CSS, and owned semantic tokens—but the
recorded evidence can select another candidate. The stack ADR decides every
layer, including runtime, D1, R2, Durable Objects, Queues/Workflows, auth,
billing, AI Gateway, text and image models, email, analytics, observability,
security, tests, CI/CD, and secrets. Cloudflare-first means “prefer the smallest
native service that fits a concrete workload,” not “use every Cloudflare
product.” All current-version compatibility must be reverified before code.

Primary references:

- [Exa Search API](https://exa.ai/docs/reference/search)
- [Exa Contents API](https://exa.ai/docs/reference/get-contents)
- [Exa contents retrieval](https://exa.ai/docs/reference/contents-retrieval)
- [Next.js on Cloudflare Workers](https://developers.cloudflare.com/workers/framework-guides/web-apps/nextjs/)
- [Cloudflare static assets](https://developers.cloudflare.com/workers/static-assets/)
- [Cloudflare bindings](https://developers.cloudflare.com/workers/runtime-apis/bindings/)
- [Cloudflare AI Gateway](https://developers.cloudflare.com/ai-gateway/)
- [Cloudflare Workflows](https://developers.cloudflare.com/workflows/)
- [Clerk Next.js quickstart](https://clerk.com/docs/nextjs/getting-started/quickstart)
- [Clerk production deployment](https://clerk.com/docs/guides/development/deployment/production)
- [Stripe webhooks](https://docs.stripe.com/webhooks)
- [shadcn/ui frameworks](https://ui.shadcn.com/docs/installation)
- [shadcn/ui for Next.js](https://ui.shadcn.com/docs/installation/next)
- [OpenAI image generation](https://developers.openai.com/api/docs/guides/image-generation)

## Install and run

```bash
python3 scripts/scaffold_product_planning_workflow.py \
  --repo /path/to/product-repo \
  --workflow product-planning \
  --version 2.1.1

HARNESS=/path/to/product-repo/.codex/workflows/product-planning/versions/2.1.1

python3 "$HARNESS/scripts/certify.py" --harness "$HARNESS"

# Prove every exact model route with bounded calls before committing to a run.
python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id planning-preflight \
  --preflight-only

python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id planning-v1

# Require live Exa evidence and tighten or expand only within the hard bounds.
python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id planning-with-live-competition \
  --research-mode exa \
  --max-competitors 6 \
  --max-pages-per-competitor 3

# Render the approved deck spec in a prepared artifact-tool workspace.
PRESENTATIONS_SKILL=/path/to/presentations-skill
DECK_WORKSPACE="$(mktemp -d)"
node "$PRESENTATIONS_SKILL/container_tools/setup_artifact_tool_workspace.mjs" --workspace "$DECK_WORKSPACE"
cp "$HARNESS/scripts/render_brand_deck.mjs" "$DECK_WORKSPACE/render_brand_deck.mjs"
node "$DECK_WORKSPACE/render_brand_deck.mjs" \
  "$HARNESS/runs/<project>/planning-v1/artifacts/05-brand-deck.json" \
  "$HARNESS/runs/<project>/planning-v1/brand-deck.pptx" \
  "$DECK_WORKSPACE/previews"

# Default speed/quality policy: Terra high at milestones, Terra medium for
# unscored lane/page/copy throughput. Score every artifact only when warranted.
python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id planning-all-high \
  --judge-policy all-high

# Cheap smoke: one unique Luna-low route preflight, then one final-only verdict.
python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id luna-low-preflight \
  --model-profile luna-low \
  --preflight-only

python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id luna-low-final-smoke \
  --model-profile luna-low \
  --judge-policy final-only \
  --max-improvement-rounds 0 \
  --seed-run /path/to/sealed/prior/run \
  --seed-mode artifacts-only \
  --skip-model-preflight

# Resume the exact same bound run after a retryable provider failure.
python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id planning-v1 \
  --resume

# Explicitly avoid Luna when that route is unavailable; this is never automatic.
python3 "$HARNESS/scripts/run.py" \
  --idea /path/to/idea.json \
  --run-id planning-sol-intake \
  --model-profile luna-free

python3 "$HARNESS/scripts/run.py" \
  --verify-run "$HARNESS/runs/<project>/planning-v1"
```

Minimal input:

```json
{
  "name": "Signal Desk",
  "idea": "A small-team workspace that turns scattered customer feedback into a prioritized weekly decision brief.",
  "target_users": ["founders", "product leads"],
  "business_model": "subscription",
  "design_preferences": ["editorial hierarchy", "no generic dashboard grid"],
  "brand_references": ["categories or references to learn from, not clone"],
  "asset_requirements": ["wordmark", "favicon", "hero art", "social card"],
  "copy_constraints": ["Do not imply that AI replaces product judgment"],
  "locales": ["en-US"],
  "required_channels": ["landing", "product"],
  "audience_language_evidence": [
    {"audience": "product lead", "phrase": "I need to explain why this is next", "source": "interview-07"}
  ],
  "claim_evidence": [
    {"claim": "Every recommendation links to its source feedback", "source": "product-contract-v1", "freshness": "reverify before launch"}
  ]
}
```

The controller defaults to four parallel Pi calls, at most 24 pages, one
semantic revision on three milestones, 80 total model calls, a 600-second run
ceiling, 6 competitors, 3 captured pages per competitor, 8,000 characters per
captured page, 180 seconds per Pi call, two attempts on classified transient failures,
and a 60-second exact-route model preflight. Every attempt counts against the
same run and wall-clock budgets. Resumes require
the identical input, resources, model profile, target, improvement rounds, and page
ceiling. Operational timeouts, retry count, parallelism, and preflight can change;
the total call ceiling may increase but never decrease. Valid
draft, judgment, pending-revision, and selected checkpoints are digest-bound.
The `luna-free` profile is an explicit operator choice, never a silent fallback.
The default `milestone` judge policy routes only the three integration milestones
to Terra high. Lanes, pages, copy packs, and other intermediate artifacts use
mechanical gates without semantic scoring. Judge payloads use deterministic
relevant-context projection where applicable; receipts expose both payload and
protocol byte counts. `all-high` is a separately bound policy and cannot resume a milestone
run. Interrupting a live run terminates every active Pi process group before the
controller writes the canceled failure record.

Treat latency as a first-class acceptance criterion. Schedule work from the
dependency DAG, not the document outline: run product, market, and design after
their shared prerequisites; run deck, audience, and framework after brand; run
messaging with stack; and run copy consistency with architecture. Do not judge
leaf artifacts by default merely because they exist. Report wall time, model
call count, retries, and per-stage timing so a slow run is diagnosable rather
than mysterious.
After a controller-only upgrade, `--seed-run /path/to/prior/run` can reuse an
exact-idea, exact-model selected artifact only when its mechanical contract,
judge ID/version, target, artifact digest, and verdict all still pass. An
unselected candidate may skip regeneration but is always rejudged. The seed
inventory digest is bound into the new run.
`final-only` mechanically verifies the complete package but semantically scores
only `20-final-plan.json`. It is intended for bounded model/controller smoke,
not promotion. `artifacts-only` seeds must have an intact provenance seal; every artifact
is revalidated under the current contracts, while every judgment required by
the new policy is produced afresh under the current model route. This permits a
safe cross-version migration: incompatible artifacts are regenerated or
repaired individually instead of rejecting a valid older seal against a newer
schema.

Under `all-high`, a first-pass score from the configured target through 0.14
points above it is a boundary result, not an automatic pass. The runtime
requests a second clean-context verdict and uses the lower deterministic
aggregate; both verdicts must clear the target. Fast milestone mode accepts the
first scored verdict to preserve its runtime budget.
Budget exhaustion, malformed output,
resource tampering, route/page/copy-pack drift, unsupported
claims, invented audience quotes, cosmetic variants, ambiguous actions, missing
page states, or post-run mutation fail closed and preserve a failure artifact.

Judge verdicts use an exact typed envelope. Before spending a model call on
contract repair, the runtime may apply only lossless mechanical normalization:
lift an exact repeated judge ID/version into the envelope and wrap a non-empty
evidence string in a one-item array. It records those operations and digests the
result. Any other malformed verdict gets at most one contract-only model repair;
anchors and evidence meaning must be preserved. A missing or unavailable pinned
model fails the run and records a receipt—there is no silent model fallback.

Before and after model-based copy repair, the controller deterministically restores only
page-owned metadata: copy-slot membership, component IDs, locations, jobs,
states, length limits, accessibility constraints, declared claim IDs, and
interaction references. It never rewrites authored copy. The keep-or-revert
ledger accepts this normalization only when it strictly reduces mechanical
errors; semantic or missing-content problems remain model work. Applying the
same normalization after repair prevents a content fix from regressing stable
page metadata. State coverage uses deterministic bipartite matching: each
required state is assigned to a distinct page slot that explicitly permits it,
so scarce states such as `loading` cannot be consumed by a greedy earlier choice.
Copy-test experiments similarly bind `one_mechanism` to the selected route
candidate's already-declared mechanism; the model cannot rename that foreign key
during synthesis or repair.
Legacy route inventories receive a deterministic conservative sitemap. A
copy-approval model call uses a patch contract rather than re-emitting every
control; structural repair receives only changed lines plus exact
`route_id:copy_id` errors. Final generation and judging use digest-bound
projections containing canonical precedence, repair deltas, and mechanically
verified route/state/control coverage, so upstream candidate copy cannot be
mistaken for the approved source of truth.
Architecture gates accept stable service IDs such as `console-worker` and
`clerk-organizations`, and they recognize the dedicated payments, AI, and image
decision objects. They verify that each required boundary was decided without
requiring a marketing display name in `services[]`.
Final synthesis is autonomous. It converts every product, design, copy,
architecture, and delivery uncertainty into the strongest conservative,
reversible default available, with its rationale, risk, fallback, and revisit
trigger. It cannot delegate those decisions as unresolved questions.
`implementation_ready` can be true only after the sitemap, selected pricing
hypothesis, exact approved copy, and every critical/high consistency resolution
pass their mechanical gates. Only facts the workflow cannot know—such as legal clearance or a live
provider-account capability—may remain in `external_verification`; each must
have an exact verification method and a safe restricted fallback. Required
external verification makes `launch_ready` false but never blocks the plan or
implementation handoff.

Every completed run writes `copy-coverage.json` and `copy-coverage.html` from
the canonical approved copy, with
route, channel, state, reader job, copy ID, exact control, claim IDs, action,
length limit, candidate count, truth/agency status, and untested-performance
status. The run seal covers both visibility artifacts.

The harness never writes application code or deploys. Its final artifact is a
sealed planning package intended to become the immutable input to a separate,
approval-gated implementation workflow.
