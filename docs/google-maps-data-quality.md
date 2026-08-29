# Google Maps data-quality phase

This phase turns Google Maps crawl observations into safe, current place data. It
is deliberately separate from menu extraction: menu verification and OCR may
remain paused without blocking place identity, opening status, or cover updates.

## Goals

- Resolve each master-data place to one stable Google Maps identity.
- Prefer deterministic evidence and reserve LLM assistance for a bounded review
  queue.
- Preserve immutable raw, normalized, validation, and decision history.
- Publish only accepted observations while retaining the last known good value.
- Keep every field traceable to Google Maps or to an explicit master-data
  fallback.

## Processing flow

```text
master mapping + Google Maps observation
                 |
                 v
       deterministic resolver
          |             |
     confident       ambiguous/invalid
          |             |
          v             v
   current mapping   bounded review queue
       overlay       (no automatic model call)
          |
          v
  validate -> decision gate -> PASS-only current place projection
                                  |
                                  v
                         downstream Neo4j publisher
```

## Deterministic resolver

The resolver compares normalized evidence without an LLM. Its evidence should
include:

- normalized place-name token similarity and known aliases;
- address and city agreement;
- distance between Google coordinates and master coordinates;
- category compatibility with the master entity type;
- canonical Google Maps URL or stable external identifier, when available;
- permanently-closed and duplicate-identity signals.

The resolver must not confirm a mapping from name similarity alone. A mapping can
be confirmed automatically only when its identity evidence is internally
consistent and no hard conflict is present. Hard conflicts, such as another city
or a coordinate distance above the configured limit, are quarantined. Ambiguous
matches enter review.

Thresholds and resolver versions must be recorded with each decision so a future
reprocess remains reproducible.

## Master-coordinate fallback and provenance

Google Maps may expose a valid detail page without coordinates in the captured
DOM or canonical URL. Missing Google coordinates do not require copying a value
and presenting it as Google evidence.

When identity is otherwise confirmed, the derived normalized observation and
current projection may fall back to the verified master coordinate. The
resulting `GeoPoint` carries explicit provenance, for example:

```json
{
  "location": {
    "latitude": 16.0544,
    "longitude": 108.2022,
    "accuracy": "verified_master_fallback",
    "source": "verified-master-data"
  }
}
```

Google-derived coordinates use `source: "google-maps-web"`. Master fallback
never changes the raw crawl or the original normalized observation; reprocessing
creates a new, traceable derived observation with its own run/observation ID.

## Bounded LLM review queue

The LLM is an optional reviewer for ambiguous identity cases, not the default
resolver and not an autonomous crawler.

- The deterministic stage writes review tasks containing only the minimum
  evidence needed for one mapping.
- Enqueueing a task never invokes a model.
- A separate, explicitly started batch may process at most the configured number
  of tasks and token budget.
- Model output is structured as a recommendation (`confirm`, `reject`, or
  `needs_human`) with confidence and evidence references.
- An LLM recommendation cannot bypass hard coordinate/city conflicts or publish
  directly to current data.
- Prompts, model/version, token usage, response, and final resolution are retained
  for audit.

Unresolved tasks remain pending. Exhausting the token budget stops that review
batch without affecting crawling or the last known good place projection.

## Current mapping overlay

Generated mappings remain reproducible outputs of verified master data. Crawl or
review results must therefore be stored in a separate current mapping overlay,
keyed by `entity_id`/`mapping_id`, rather than modifying the generated registry in
place.

An overlay records, at minimum:

- canonical Google Maps identity and URL;
- resolved Google place name and aliases;
- mapping status and confidence;
- resolver version and resolution timestamp;
- supporting source-record and validation identifiers;
- reviewer metadata when human or LLM review was involved.

At runtime, the generated registry is loaded first and the overlay is applied
second. A rejected or quarantined overlay entry makes the mapping ineligible for
automatic publication.

## Current place projection

`CurrentPlace` is the downstream read model for the latest accepted place data.
It is not an event log and must not replace raw or normalized history.

Publication rules:

- only a `PASS` decision can update `CurrentPlace`;
- the observation and decision must refer to the same place and run;
- an older observation cannot replace a newer current value;
- the update is atomic and idempotent;
- a failed, review, quarantined, empty, or stale crawl leaves the previous current
  value untouched;
- permanently closed is a valid status update when supported by accepted evidence;
  the place is archived/marked closed rather than deleted;
- every published field retains its source and observation timestamp;
- master coordinates may be used only under the provenance rule above.

The minimum current projection should contain the resolved name, business status,
today/weekly opening data when available, address, location with provenance, one
cover image when available, source URL, observation/decision IDs, `observed_at`,
`updated_at`, and freshness metadata.

## Reprocessing and rollout

Reprocessing consumes existing immutable observations; it does not crawl Google
Maps again merely because resolver logic changed. Run it with:

```powershell
python -m nextrip_pipeline.cli reprocess-google-maps `
  --manifest config/google-maps-batch-manifest.json `
  --observation-root data/normalized
```

This creates bounded semantic-review requests but does not call a model. Add
`--disable-llm-review-queue` for deterministic-only maintenance reprocessing.

Rollout order:

1. run the resolver against existing observations;
2. inspect resolver coverage, review, and quarantine counts;
3. process only the bounded ambiguous queue as needed;
4. publish `PASS` observations into the current mapping overlay and
   `CurrentPlace`;
5. complete missing place crawl coverage;
6. enable scheduled refresh and downstream Neo4j publication.

Menu source discovery may still be captured opportunistically, but menu OCR and
menu verification remain outside this phase's critical path.
