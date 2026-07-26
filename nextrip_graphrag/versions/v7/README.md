# GraphRAG V7

V7 removes phrase aliases, query regex templates, and deterministic intent
fastpaths from the production planner.

The query path has three explicit stages:

1. Gemini structured output extracts a typed intent and raw semantic mentions.
2. Graph candidates ground cities, areas, places, categories, and concepts.
3. The existing V5 typed executor retrieves only from validated canonical values.

V7.1 retrieves place candidates independently from the full-text and vector
indexes, then combines their ranks with reciprocal rank fusion. Raw scores remain
in the trace for diagnostics but are never mixed because the two indexes do not
share a calibrated score scale. Candidate retrieval and LLM selection are
separate components, so grounding can be tested without invoking the planner.

Exact matching is limited to normalization of values already present in the graph
catalog. Non-exact values require candidate retrieval and an LLM selection from a
closed candidate list. Unknown or ambiguous values block retrieval and are exposed
in the trace instead of being guessed.

The proposed graph-index evolution and retrieval-mode routing are documented in
`docs/GRAPHRAG_V8_ARCHITECTURE.md`.
