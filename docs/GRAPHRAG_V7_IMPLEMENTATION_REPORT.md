# GraphRAG V7 Implementation Report

Date: 2026-07-25

## Outcome

V7 introduces an LLM-native planner and removes the V5 lexical planner from the
V7 production path. V5 and V6 remain available as legacy baselines.

The V7 path is:

1. Gemini structured semantic parsing.
2. Closed-candidate graph entity and concept grounding.
3. Existing typed V5 graph execution.

V7 has no deterministic intent fastpath, query regex template, alias table, or
catalog fallback. When Gemini is unavailable, the response is explicitly marked
`planner_unavailable`.

## Grounding contract

- Entity types, intent values, requested fields, ranking criteria, constraints,
  and tool names are validated by Pydantic enums or typed models.
- Place mentions are grounded using Neo4j full-text and vector candidates.
- City, geo-area, and category values are selected from the graph catalog.
- Concept meanings reuse the V5 embedding shortlist and constrained LLM
  selection.
- The LLM selector can return only a candidate ID supplied by the application.
- Unknown, ambiguous, low-confidence, or out-of-whitelist values block
  retrieval instead of being guessed.
- Trace output records the raw mention, candidates, selected canonical value,
  method, score, confidence, and unresolved values.

## Integration

- API `kb_version` accepts `v7`.
- CLI commands: `v7-query` and `v7-benchmark`.
- Registry manifest includes `llm-semantic-grounded-router-v7`.
- V7 reuses the verified V5 graph snapshot and typed executor.
- Backend version contracts and typed-KB routing accept V6 and V7.
- Frontend version selection exposes V6 and V7; its generated OpenAPI TypeScript
  contract was refreshed.

## Verification

- Full unit suite: 138 passed.
- Focused V5/V6/V7 regression suite: 46 passed.
- Backend suite: 51 passed with the test contract pinned to V3.
- Frontend suite: 6 passed; production TypeScript/Vite build passed.
- Python compilation: passed.
- Anti-hardcoding test rejects V7 source imports or declarations for planner
  lexicons, alias constants, subject-pattern constants, or query regex.
- Candidate-whitelist test confirms invented selector IDs remain unresolved.

## Live benchmark status

A valid V7 accuracy score has not been produced yet because the configured
Google Vertex AI project returns:

`403 PERMISSION_DENIED / BILLING_DISABLED`

The configured preflight was rerun against case `A-001` from the requested
500-case benchmark on 2026-07-25. It returned the non-retryable planner reason
`GeminiBillingDisabled`, so the runner intentionally did not manufacture 500
fallback results.

The configured Neo4j Aura hostname also fails DNS resolution. A local V5 Neo4j
container is running, but the current workspace credentials do not authenticate
against it. V7 deliberately does not replace either dependency with phrase rules
or expected-output fixtures.

Once Gemini billing and Neo4j connectivity are restored, run:

```powershell
python -m nextrip_graphrag v7-benchmark `
  --planner-mode configured `
  --source "../docs/test_cases_benchmark_v3 (1).md" `
  --output "nextrip_graphrag/evaluation/results/benchmark_v3_v7.json"
```

The existing V6 score of 471/500 remains an in-sample legacy reference, not a V7
result.
