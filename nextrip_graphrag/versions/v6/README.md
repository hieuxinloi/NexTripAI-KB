# GraphRAG V6

V6 keeps the verified V5 ontology and indexes. It changes the query layer:

- explicit conversation context instead of relying on implicit global state;
- standalone resolved queries recorded for every follow-up turn;
- deterministic itinerary scheduling from graph-grounded recommendations;
- opening-hour and suggested-duration checks when those properties exist;
- preference relaxation is explicit in `missing_fields` and `trace`;
- V5 remains available for direct regression comparison.

The language planner still uses Gemini structured output when configured and the
validated V5 deterministic fallback when the planner is unavailable. V6 does
not contain testcase IDs, expected answers, or place-specific routing branches.
