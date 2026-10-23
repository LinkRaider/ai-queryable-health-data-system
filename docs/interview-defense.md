# Interview defense
A speaking guide for João

## 1. Scenario and goal
**Cues:** heart rate; historical snapshot; source → database → queries.
**Explain:** “I built a small end-to-end system that turns synthetic heart-rate observations into queryable data. Heart rate demonstrates irregular sampling, workouts, day/night patterns, and device gaps. The core generates a historical snapshot, preserves its source, and loads PostgreSQL. Queries work through the CLI; the HTTP and natural-language layers are optional additions. The sample illustrates engineering choices, not clinically validated physiology.”
**Open:** src/health_data/: generator.py → source.py → db.py → query.py → cli.demo_queries.

## 2. Generation — volume, sampling, and realism
**Cues:** 50 users; seven days; 271,620 observations in the default sample.
**Explain:** “The default seven-day sample has 50 synthetic people and 271,620 readings: users and observations are different counts. This shows varied routines, local dates, workouts, and gaps in a manageable demo. Nominal sampling intervals are 300 seconds asleep, 180 resting, 75 active, and 20 during exercise, with variation. When a device is off, there is no observation—not a zero reading.”
**Why this design:** profiles vary schedules, time zones, resting baselines, activity responses, and device availability. A fixed seed and window make the sample repeatable; different configurations can produce different totals. Repeatability supports testing, not recovery of real measurements.
**Open:** generator.build_day_plan, _heart_rate_value, generate_events, HeartRateEvent; docs/synthetic-sample.md. Events carry version, stable ID, user/device, offset-aware timestamp, metric, value, unit, and state.

## 3. Ingestion — why batch
**Cues:** publish source first; transactional batches; full replay; stable identity.
**Explain:** “The input is a week generated together, so batch is simpler than streaming. I publish the complete JSONL source before ingestion, then read it from the beginning in batches of 1,000. JSONL means one JSON event per line. Each batch commits independently. On retry, existing observations are checked and skipped, and missing ones are inserted. The same identity with different content fails instead of silently overwriting data. Recovery retries retained original events; it does not generate replacement measurements.”
**Trade-off:** replay cost grows with source volume. Here retained input and retry-safe writes keep recovery simple without a saved source position.
**Open:** cli.bootstrap → source.ensure_source → db.ingest → _assert_batch_matches_database.

## 4. Storage — layer, formats, schema, and partitioning
**Cues:** inspectable JSONL; typed PostgreSQL rows; constraints and indexes.
**Explain:** “JSONL preserves the original input for replay and is easy to inspect. PostgreSQL gives me typed observations, constraints, transactions, and relational queries in one place. users holds profiles and time zones; heart_rate_observations holds readings; ingestion_runs records source identity and outcomes. The stable UUID key supports replay; the natural unique key prevents two IDs for the same user/device/timestamp/metric. Indexes follow user/time and time/state/user queries.”
**Trade-off:** a heart-rate-specific table makes validation clear, but another metric needs a migration. Indexes cost space and write work. For 50 users over seven days, one indexed table supports the user/time and daily aggregation queries; partitioning would add maintenance without a demonstrated query or retention benefit.
**Open:** sql/schema.sql; db.apply_schema; source.publish_source. Atomic rename publishes the source, followed by a metadata sidecar marking completion. Both are validated before reuse; incompatibility or corruption fails. A different source configuration needs an isolated database. A retained volume survives container replacement, but is not a backup.

## 5. Queries — engine and analytical meaning
**Cues:** five operations; arithmetic mean of readings; local versus UTC days.
**Explain:** “PostgreSQL also runs the aggregations because these access patterns fit relational SQL; another engine would add synchronization without a demonstrated benefit. user_period_average returns one mean over all of one user's recorded readings in the interval. user_period_resting_average returns one mean over only rest readings. daily_user_average still averages that user's resting readings by local day. activity_state_comparison compares one user's readings by state. population_daily_average averages all readings by UTC day. Every mean is the arithmetic mean of the recorded observations: each reading contributes equally. Higher-cadence states and people with more readings therefore carry more weight; this is neither time weighting nor equal-person weighting.”
**Bounds and missing data:** the dataset spans seven days; the maximum query range is 31 days. A query selects existing observations only. [start, end) includes the start instant and excludes the end. No matches returns HTTP 200 with an empty rows list, never an invented zero mean. row_limit counts aggregated output rows, not raw readings: four states with row_limit=2 return 413, not partial rows. The default 1,000-row ceiling is not normally reachable with these operations and the 31-day range. Counts and first/last contributing timestamps describe readings, not continuous coverage. Seven UTC days can overlap eight local dates.
**Open:** query.validate_query → build_query → execute_query; the four SQL constants; tests/test_query.py for hand-calculated expectations. The application owns the SQL and binds request values as parameters.

## 6. Complete startup with Compose
**Cues:** five services; automatic preparation; one startup command.
**Explain:** “docker compose up --build starts postgres and ollama, runs bootstrap to publish and ingest the source, and runs model-init to download the model. api starts after those preparation jobs finish. The README documents Docker/Compose, supported architecture, disk space, and first-start network access. The user's native Mac report includes empty-volume startup, replay, and restart checks.”
**Current readiness:** /health/ready checks completed ingestion, existing observations, and model listing, not actual inference. Once the API is running, structured queries do not call the model; Compose startup and readiness still depend on this bonus.
**Open:** compose.yaml; scripts/clean_start_e2e.sh; api readiness handlers.

## 7. README, submission, and evaluation
**Cues:** public repository; actual architecture; decisions and trade-offs; five working days.
**Explain:** “The submission is the public GitHub repository. Its README includes the architecture diagram, prerequisites and startup commands, design decisions, two depth areas, AI-use notes, and one closing future-work paragraph. docs/specification.md describes the implementation's chosen contract. Within five working days, the aim is a smaller working system with defensible choices—not the largest stack. I would demonstrate runnable code, explain trade-offs, and show where human judgment shaped the AI-assisted work.”

## 8. Bonus 1 — structured HTTP queries
**Cues:** explicit request and response; shared query core; useful errors.
**Explain:** “POST /v1/query accepts operation, start, end, metric, user_id, and row_limit. Pydantic checks shape and rejects extra fields; domain validation checks meaning. The route calls the same execute_query as the CLI. Responses expose the operation, interval, aggregation basis, and rows. The contract is the accepted inputs, their meaning, outputs, and errors—not just field names.”
**Errors:** 422 for invalid requests, unsupported queries, unknown users, or rejected plans; 413 for oversized results; 503 for unavailable database or model dependencies. Errors should explain the failure without leaking credentials or internal implementation details. OpenAPI documents the interface; it does not authenticate users or enforce access. This is a local demo interface.
**Open:** api.StructuredQueryRequest.to_domain; database_connection; create_app.

## 9. Bonus 2 — natural language and model choice
**Cues:** local qwen2.5:1.5b through Ollama; candidate JSON; validated structured path.
**Explain:** “I chose a small local model to avoid paid API access and keep inference local. Its output is validated before reaching the query engine. POST /v1/query/natural-language first checks supported vocabulary and resolves dates in the application. The model proposes a JSON plan, not SQL. I validate its schema, identity, dates, operation, and recognized cues before passing it to the structured query path. The response includes the interpretation and results, so the user can see what was answered.”
**Scope:** constrained English and five operations, not general language understanding. ‘last week’ means the previous ISO week relative to request time in UTC; it can fall outside the historical sample and return no rows. ‘my’ without explicit identity is rejected. Unsupported metrics, aggregations, groupings, incompatible filters, and medical advice are rejected rather than guessed. The brief's disrupted-sleep example is outside this heart-rate implementation.
**Open:** natural_language.validate_question → resolve_temporal_expression → OllamaPlanner.plan → interpret_model_plan → query.execute_query. Extra fields, including sql, are forbidden. A well-formed plan can still be wrong; bounded lexical validation reduces known mismatches, not all semantic errors.

## 10. Two areas of depth
**Cues:** persisted-source recovery; transparent analytics.
**Explain:** “The first depth area is replay from retained source: reuse without regeneration, accept exact duplicates, reject conflicting payloads, and preserve earlier committed batches after interruption. The interruption check showed replay completing the same dataset after committed progress. That establishes this recovery case, not protection from losing the source volume. The second is precise analytical semantics under irregular sampling: observation weighting, local and UTC grouping, contributing timestamp bounds, and absence rather than zero. Natural language remains a bonus, not an extra depth claim.”
**Open:** scripts/test_interrupted_ingestion.sh; tests/test_ingestion.py; tests/test_query.py for recovery and hand-calculated query cases.

## 11. AI use — delegation and human judgment
**Cues:** substantial implementation assistance; concrete design correction; acceptance stays human.
**Explain:** “Scaffolding, implementation drafts, tests, Compose wiring, and documentation structure had substantial AI assistance. My concrete intervention was challenging recovery through deterministic regeneration and the positional checkpoint complexity around it. Real measurements cannot be recreated if they were never retained. I chose a persisted JSONL source and full idempotent replay, keeping PostgreSQL batches without permanent extra services; implementation of that revision was delegated. I also challenged rejecting a reasonable whole-period user average. The extension now answers it explicitly, preserving rest-only intent and leaving daily semantics unchanged. My contribution was correcting the problem framing and analytical meaning, not personally typing every function.”
**What would you not trust AI to do, and why?** “I would not blindly delegate the failure model, acceptance of recovery guarantees, or infrastructure scope. AI can build a coherent answer to the wrong problem. I need to ask what survives a failure, which original events exist, and whether a checkpoint or broker earns its complexity. I also need independent counterexamples for analytical meaning and a check that delivery claims match observed behavior.”
**Other review concerns:** scope, plausible synthetic patterns, query/error boundaries, and final claims. The development record also includes corrections to Python compatibility, the packaged schema path, image ownership, API dependencies, and rejected model plans. Clinical, privacy, and security decisions require appropriate domain review.

## 12. “If I had two more weeks”
**Cues:** secure access and first inference; measure capacity; conditional infrastructure; tested recovery.
**Explain:** “Before public exposure, I would establish authentication for identity, per-user authorization for data access, TLS for transport protection, rate limiting against abuse, and access auditing for traceability. I would add separate readiness checks: core means validated PostgreSQL queries work; inference means a supported English question can become a plan within the timeout. These are proposed checks, not current features. I would expand users, history, and seed variation to measure ingestion, replay, query latency, and storage. Time-based partitioning would become useful if measured query pruning or retention deletion warrants it. Parquet would suit larger analytical scans or long-term archival; a lakehouse would need a shared-data requirement across analytical engines. If continuous arrivals become a requirement, I would explore streaming and consider Kafka only for concrete retained replay, backpressure, and independent-consumer needs. Within two weeks I would prioritize validation and tested backup/restore paths rather than promise every extension.”

## 13. Follow-up questions
**Is this exactly-once?** “Retrying the same event leaves one database observation; a distinct new measurement has its own identity. That is idempotent replay with conflict checks and per-batch transactions, not general end-to-end exactly-once delivery.”
**What would streaming change?** “A continuous producer and consumer would replace the bounded input path. A broker adds operational cost. If Kafka is justified, I would choose a user/device ordering key and retention horizon, then commit consumer offsets only after database commits. Redelivery still needs stable identities and an idempotent sink.”
**Why can the average mislead?** “Workout readings arrive more often, so they contribute more. A reading average is not time spent in each state or an equal-person average.”
**How is a week-wide mean calculated?** “The period average is calculated directly from all matching observations, not from previously calculated daily averages. Three rest readings averaging 60 BPM on one day and one at 70 on another give 62.5 BPM overall, not 65. A question without grouping selects a period mean; ‘resting’ selects rest-only, and ‘each day’ selects daily rest means. No matches stays an empty result.”
**Can readiness pass before a model request fails?** “Yes: listing the model does not exercise inference. A first request can exceed 30 seconds and return 503; a later loaded-model request may succeed.”
**What remains to validate?** “Broader clean-machine deployment, production load and concurrency, source-volume loss, backup/restore, and production security. The interruption check covers progress already committed, not an interruption deliberately synchronized inside an open transaction.”
**What stays without the bonuses?** “Generation, retained source, ingestion, PostgreSQL queries, and CLI demo-queries. Removing API, natural language, and Ollama also requires updating Compose, dependencies, tests, scripts, and documentation.”

## Quick reference
Original challenge: AI-Queryable Health Data System. Repository: https://github.com/LinkRaider/ai-queryable-health-data-system
The separate code-explanation guide provides file-by-file navigation.
