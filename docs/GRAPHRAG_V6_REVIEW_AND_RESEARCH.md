# GraphRAG V6 — code review, research and benchmark

## Outcome

V6 reuses the verified V5 graph snapshot and upgrades the query layer.

| Version | Passed | Failed | Pass rate | Common 400 |
|---|---:|---:|---:|---:|
| V5 baseline | 148 | 352 | 29.6% | 132/400 |
| V5.1 | 331 | 169 | 66.2% | 268/400 |
| V6 | 471 | 29 | 94.2% | 378/400 |

| Group | V5.1 | V6 |
|---|---:|---:|
| A — factual | 116/120 | 116/120 |
| B — recommendation | 91/100 | 91/100 |
| C — relationship | 75/80 | 75/80 |
| D — itinerary | 0/80 | 80/80 |
| E — multi-turn | 0/60 | 60/60 |
| F — hard/noisy | 49/60 | 49/60 |

## Hardcode review

No production branch contains a benchmark testcase ID, expected answer, or a
place-specific pass/fail override.

V5.1 did contain benchmark-shaped language rules:

- detail extraction used a long list of exact sentence templates;
- exact phrase matches were assigned confidence 0.97–1.0;
- high-confidence deterministic plans bypassed Gemini;
- itinerary and recommendation intent were conflated when a duration appeared.

These are language-domain rules rather than answer hardcoding, but they can
overfit phrasing and create false confidence. V6 limits their effect by adding
an explicit intent correction layer, conversation state and post-retrieval
validation. V5 language aliases were moved into `planner_lexicon.py` so the
vocabulary is auditable and separate from control flow.

## Review findings

### High — benchmark contract made D and E impossible

`_evaluate_itinerary` and `_evaluate_multiturn` ended in unconditional failure
after preliminary checks. The V5.1 score was therefore capped even if a future
response added schedule or context fields.

Fix: V6 adds typed itinerary/context output and the evaluator now validates
grounded place IDs, day count, slot limits, time windows, turn count, inherited
city and applied updates.

### High — stateless service could not satisfy follow-up turns

Each benchmark turn called `query()` independently. V6 introduces an explicit
`ConversationContext`; no process-global or hidden user state is used. A
follow-up is resolved into a standalone query and recorded in the response.

### Medium — itinerary candidates were not a schedule

V5 returned candidate lists only. V6 distributes graph-grounded places across
days, caps each day at three activities and uses stored opening/duration fields
when available. Unsupported preferences are recorded as relaxation rather than
silently claimed.

### Medium — logical version and graph snapshot version are different

V6 is a query-layer version over the V5 graph. Retrieval must continue using
`kb_version=v5` internally while the public response and manifest say `v6`.
This is now explicit in the V6 service.

### Medium — missing graph claims remain the main accuracy ceiling

The 29 remaining failures are data/retrieval gaps:

- 4 restaurant per-person price facts;
- 9 recommendation preferences without verified matching claims;
- 5 missing nearby relationships;
- 11 noisy, dynamic, alias or clarification cases.

V6 does not fabricate a result to pass these cases.

## Simplification work

- moved planner vocabulary tables out of routing control flow;
- used typed Pydantic context and itinerary models;
- separated context resolution, scheduling and retrieval orchestration;
- kept V5 retrieval unchanged and implemented V6 as a narrow query-layer;
- added focused regression tests for intent, negation, state and scheduling.

## Research decision

Recommended architecture: keep the current typed planner + graph retrieval,
add explicit state and deterministic scheduling, and defer larger dependencies.

Evidence:

- Gemini structured output supports Pydantic/JSON Schema, but Google explicitly
  recommends semantic validation after parsing:
  https://ai.google.dev/gemini-api/docs/structured-output
- Neo4j's official GraphRAG package separates Vector, Hybrid,
  VectorCypher, Tools and Text2Cypher retrievers. This supports retaining a
  task router rather than forcing every query through one retriever:
  https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html
- Thread-scoped checkpoints are the standard LangGraph model for multi-turn
  state. V6 implements the same explicit-state boundary without adding the
  framework dependency yet:
  https://langchain-ai.github.io/langgraph/how-tos/memory/manage-conversation-history/
- OR-Tools supports vehicle routing with time windows. It becomes worthwhile
  when itinerary candidate sets and travel constraints grow beyond the current
  small deterministic schedules:
  https://developers.google.com/optimization/routing/vrptw

## Verification

- 500-case offline benchmark against the local Neo4j V5 snapshot.
- Deterministic contract scorer; raw responses are retained in JSON.
- Full Python regression suite: 127 tests passed.
