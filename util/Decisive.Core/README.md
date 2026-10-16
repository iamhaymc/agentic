# Decisive

*Product selection from cleaned, structured media content, resolved product metadata, and LLM or SystemOne decisions.*

[CORE](Core/) | [TOOLS](Tools/) | [TESTS](../Decisive.Tests/)

> Status: library design scaffold. The core project targets .NET 8; source files and the test project remain empty placeholders. Descriptions below define planned responsibilities, not implemented classes or working APIs.

## Build (Core)

Restore and build the package-backed project from the repository root:

```powershell
dotnet restore util\Decisive.Core\Decisive.Core.csproj
dotnet build util\Decisive.Core\Decisive.Core.csproj --no-restore
```

Dependency policy:

- All 23 requested packages use explicit stable versions; no floating references.
- ASP.NET Core SignalR, Entity Framework Core, and Serilog.AspNetCore stay on their .NET 8 release lines.
- LLamaSharp and both native backends share version 0.27.0; runtime backend selection remains part of the planned library implementation.
- Package availability does not imply implemented integrations; no cloud, model, or document-processing APIs run during the build.

Project setup checklist:

- [x] Minimum Scope: replace package URLs with an SDK-style .NET 8 class library.
- [x] Complete Scope: reference all 23 requested packages with compatible, explicit versions.
- [x] Extended Scope: verify package restore and a warning-free build; leave library implementation out of scope.

The reference is Agentic's media ingestion, LLM extraction, metadata binding, and bounded tool loop. Decisive deliberately removes product knowledge from extraction: MediaImport reads the media, ProductQuery identifies and fetches relevant product data, and SelectionBinder binds and reconciles against that data. Explicit exit and export tools are proposed additions.

**Selection flow: MediaImport -> cleaned, structured content -> ProductQuery -> fetched product metadata -> SelectionBinder -> validated selections -> MediaExport.**

MediaImport uses LLMs to recover as much source content as possible without product data, catalog access, or product-specific extraction schemas. ProductQuery uses SystemOne (SYS1), an LLM, or both to detect product identifiers and search hints in that content, then fetches authoritative metadata. SelectionBinder uses deterministic logic or SYS1 decisions for binding and reconciliation. Product metadata and product rules still determine which fields, values, and combinations are valid; model confidence alone does not establish validity.

ProductQuery calls CriteriaResolver internally to assemble source content into scoped requirements. SelectionBinder searches and evaluates candidate configurations against those requirements and calls SelectionRanker to order feasible alternatives. CriteriaResolver and SelectionRanker are internal selection helpers located alongside the tools; neither derives from HarnessTool or registers with the harness.

Selection is per entity, not a joint optimizer for a collection of products. Shared budgets, aggregate targets, and cross-product compatibility remain explicitly unsupported unless the host replaces them with authoritative, independently applicable criteria in a new revision; Decisive never claims to have validated the combined set.

## Govern (Pipeline)

Shared rules for every stage and its implementation checklist:

- **Close evidence scope before recommending:** MediaImport records expected, read, unread, and uncertain source regions without judging product relevance. CriteriaResolver associates those regions with entities and criteria. Unread content or unresolved applicability blocks verification for every potentially affected entity; unknown scope conservatively affects all entities. A source-linked reviewer response may establish non-applicability, but processing every region establishes coverage only, not accurate transcription or interpretation.
- **Track fidelity independently of coverage:** material facts and interpretations carry `model_interpreted`, `source_checked`, or `authoritative_input` assurance with witness references and revisions. Deterministic checks against original text/cells must cover the relevant value, qualifier, and scope; otherwise a recorded reviewer check is required for source confirmation. Explicit host-authorized criteria are authoritative inputs. Model confidence, repeated model agreement, and a real catalog match cannot themselves promote assurance. Checks concern fidelity to the source or authorized criteria, not whether the original document is factually correct.
- **Separate eligibility from ranking:** `criteria_compliant` means the evaluated selection satisfies the interpreted criteria snapshot; it does not certify the source reading or authorize a recommendation. A verified recommendation means `source_verified`: all material evidence used for identity, scope, criteria, and ranking is source-checked or authoritative, evidence scope is closed, metadata is current, product rules are valid, mandatory criteria pass, and hard review flags are resolved. Other results remain provisional regardless of score; assurance and limiting reasons must be displayed.
- **Keep independent selection explicit:** document-wide criteria may apply separately to each entity, but shared totals, allocation constraints, and cross-product compatibility are not copied into independent per-item checks. Represent these as `unsupported_coupled_constraint` with unknown compliance and block verified recommendations for affected entities or the collection. Unaffected entities may still qualify individually. A host-supplied allocation creates a new criteria revision but does not establish that Decisive verified or optimized the full set.
- **Keep search hypotheses soft:** detected brands, families, and model numbers prioritize search or trigger focused lookup; record existence proves only that the catalog entry exists. Hard exclusion filters require a host authorization rule, a source-confirmed mandatory criterion, or a sound metadata/rule proof of incompatibility. Record each filter's origin, scope, and evidence. Exhausting a hint-selected subset does not prove no match within the authorized catalog scope; broaden the search or report its limitation.
- **Distinguish products from configurations:** a catalog candidate identifies a product/model record and metadata version supplied by ProductQuery. A selection candidate identifies one concrete field/option assignment for that product, criteria revision, and metadata/rule snapshot. SelectionBinder owns bounded, rule-guided exploration of alternative configurations; one failed assignment does not reject an entire product.
- **Limit search claims to tested scope:** catalog lookup and configuration exploration each report `complete`, `limited`, or `unknown` coverage, filters, continuation state, and stop reason. Only exhaustive evaluation or sound rule-based exclusion can prove absence within that declared scope. Otherwise return `no_verified_candidate`, not `no_feasible_candidate`; a feasible result from limited search is only the best among evaluated alternatives, not a proven global optimum.
- **Keep the compliance baseline immutable:** CriteriaResolver publishes a versioned criteria snapshot. Binding and reconciliation change proposed selection inputs, never that snapshot. Evaluate final values against the unchanged snapshot, preserving every original criterion and its source. A host-authorized correction or requirement change creates a new revision and invalidates affected downstream results; reconciliation cannot relax requirements.
- **Obtain evaluated values from authoritative providers:** static values may come from versioned catalog records; calculated capacity, efficiency, or other scenario-dependent results come from an injected host evaluation provider. Requests identify configuration, requested metrics, units, operating conditions, and metadata/rule versions; responses retain values, applicable conditions, provider/model version, and validity limits. Missing conditions, unsupported metrics, unavailable providers, and out-of-domain results remain unknown or explicit errors, never inferred numeric values from SYS1. Compare and rank results only under compatible conditions and units.
- **Make abstention explicit:** stage outcomes distinguish `matched`, `ambiguous`, `needs_evidence`, `no_match`, and `provider_error`. Named-choice calls that may lack a supported answer include a reserved non-catalog abstention option within the provider's option limit; callers translate it to an unresolved outcome and never bind it as a field or option. Missing evidence, conflicting matches, or an unmet declared threshold must not become an arbitrary choice; transport errors remain errors, not abstentions.
- **Define evidence and review handoffs:** a request carries its ID, run/input revision, affected entity and criterion IDs, source/region references, reason, question, accepted response shape, and resume stage. A reread asks MediaImport for generic source regions, never product-specific extraction. The host supplies new media, evidence, a correction, or an explicit policy decision through the same typed contract.
- **Resume by revision, not by mutating a finished run:** HarnessBase returns `awaiting_evidence` or `awaiting_review` with a checkpoint; _Agent accepts matching responses and starts a new bounded execution for the logical run. Reject stale or duplicate responses, retain consumed model budgets, and rerun only invalidated stages. Approval alone cannot turn failed or unknown mandatory compliance into a pass. Checkpoint persistence is host-provided; the library does not imply durable recovery.
- **Bound downstream model context:** interpretation, lookup, binding, and ranking partition evidence and page metadata just as extraction chunks media. Preserve source links and include applicable shared criteria in each task; merge outputs against the full criteria snapshot. Retaining full typed data does not authorize sending it in one request. Split oversized requests or return an explicit context-limit outcome; never silently truncate evidence.
- **Share one run budget:** HarnessBase owns a concurrency-safe ledger for model attempts, input tokens, and reserved/output tokens across both providers, internal helpers, retries, and resumed executions. Reserve capacity before dispatch, retain a conservative reservation when usage is unknown, and reconcile reported usage. AppSettings defines context limits, per-stage allocations, and remaining-work reserves. Exhaustion returns `budget_limit` with completed work and unsearched scopes, not a successful empty result.
- **Version dependency artifacts:** extraction, criteria, metadata, evaluations, rankings, and exports record their source/content revision, parent artifact IDs, applicable policy/model configuration versions, and metadata/rule snapshots. A changed dependency invalidates its descendants, not unrelated work. Pin a consistent metadata/rule snapshot during evaluation; refresh starts a new dependent revision. Superseded artifacts remain historical evidence but cannot feed current recommendations or exports.
- **Finalize every exit from typed outcomes:** normal model completion, ControlExit, awaiting states, and execution limits all use one deterministic finalization path. Public summaries derive identity, recommendation, assurance, and search claims from current typed artifacts; unrestricted model prose is diagnostic only. Missing typed output for the requested operation yields an incomplete result, not an invented answer. Structured no-match or no-feasible outcomes remain valid reported outcomes without becoming recommendations.
- **Publish under a current revision atomically:** one authoritative generation token identifies the current logical-run revision and its accepted dependency versions. Source, criteria, policy, metadata, or evaluator-data changes advance it and invalidate affected results. Stage output or export bytes first, then atomically compare the expected token, recheck dependency freshness, and publish the current artifact reference in that same serialized boundary. A lost race returns superseded, not current; a later change marks a previously published artifact historical. HarnessBase owns this boundary in memory; hosts sharing or persisting a logical run must provide the same conditional-publication contract. If freshness or atomic publication cannot be established, expose only a provisional/historical artifact, never a current verified result.
- **Make repeated operations idempotent:** within a logical run, identify an operation by its intent ID, canonical arguments, and dependency revisions, not the model's tool-call ID. Concurrent duplicates share one execution and completed duplicates reuse the current result. Changed inputs or intentional repetition require a new revision or intent ID. Never reuse failed or stale results as success; cross-run reuse requires an explicit host policy.
- **Make export retries safe:** pass the operation's idempotency key, expected generation token, and result revisions to the host writer/publication boundary. A retry returns the confirmed artifact with its current/historical status or continues a host-verifiable pending write, never blindly duplicates it. A completed byte write is not confirmation of current publication; an unconfirmed write remains indeterminate until resolved. Dependency changes mark prior exports superseded without deleting historical files.

## Understand (Classes)

Class roles follow existing source files in Core, Tools, and Tests. Tools contains both registered HarnessTool implementations and the unregistered CriteriaResolver and SelectionRanker helpers; directory placement does not imply tool registration. Unchecked features form the implementation plan; reference behaviors and regression lessons are requirements, not claims that Agentic has solved every edge case.

Implement shared contracts and settings first, clients and harness next, then product-independent extraction, requirement resolution, metadata lookup, candidate binding and evaluation, ranking, and remaining tools. Add offline tests with each feature; run live benchmarks only when explicitly selected. Populate both project files and wire the test project's reference to Core before compiling.

### **_Agent** ([Core/_Agent.cs](Core/_Agent.cs)): 
Public entry point for a product-selection request. Assembles settings, model clients, host-provided product access, and registered tools, then starts the harness and returns its structured result. Keeps application wiring separate from extraction and binding logic; leaves storage, authorization, and user interaction to the host.

Implementation checklist:

- [ ] Inject inference and decision clients, metadata/rule providers, an optional host evaluation provider, and authorized resource resolvers; avoid hidden global configuration or run state.
- [ ] Preflight dependencies for the requested stage; allow MediaImport without any product services and check catalog or rule availability only when querying or selecting products.
- [ ] Register only tools supported by the supplied dependencies; create an isolated context for each request, including concurrent requests.
- [ ] Compose CriteriaResolver into ProductQuery and SelectionRanker into SelectionBinder; keep both helpers off the harness tool registry.
- [ ] Retain typed extraction, metadata-resolution, and selection outcomes under run-scoped IDs; pass full data between tools by reference rather than reconstructing it from model summaries.
- [ ] Return the common finalizer's deterministic, generation-checked response for every completion path; keep raw model explanations in clearly non-authoritative diagnostics.
- [ ] Expose typed evidence/review requests and revision-checked resume inputs to the host; accept optional host checkpoint storage without adding UI or durable-workflow dependencies.
- [ ] Forward stage progress through an injected reporter without UI or WebSocket dependencies; log reporter failures without discarding selection work.

### **AppSettings** ([Core/AppSettings.cs](Core/AppSettings.cs)): 
Configuration shared by the agent, model clients, and tools. Defines separate decision and inference configuration sets, each containing Model ID, API URL, and API Key, plus request timeouts, run limits, extraction concurrency, allowed product families, and review settings. Validates settings before work starts and accepts credentials from the host rather than embedding them in source.

Implementation checklist:

- [ ] Define the decision set as `DecisionModelId`, `DecisionApiUrl`, and `DecisionApiKey`, and the inference set as `InferenceModelId`, `InferenceApiUrl`, and `InferenceApiKey`.
- [ ] Use the inference set for both text and image requests when supported; do not define a separate vision Model ID, API URL, or API Key set.
- [ ] Validate endpoint URLs, finite temperatures, positive limits, and supported option combinations; reject invalid values rather than silently clamping them.
- [ ] Separate extraction settings from product-query and binding settings; configure deterministic or SYS1 binding and reconciliation without giving MediaImport product-specific options.
- [ ] Accept explicit source-precedence and ranking policies, including preference weights and stable tie-breakers; do not infer authority from document order or invent selection objectives.
- [ ] Define material-evidence check requirements and host evaluator timeouts/concurrency; keep per-entity scope fixed and do not add model/API connection sets for host-supplied evaluation services.
- [ ] Separate text and vision output budgets, retry budgets, model-call concurrency, page-reader concurrency, and CPU-bound binding concurrency; bound SYS1 work separately from CPU work.
- [ ] Define per-request context limits and shared logical-run model-attempt, input-token, and output-token limits, with stage allocations and reserves; include retries and resumed work in the same budget.
- [ ] Bound catalog pages and configuration trials; configure deterministic or SYS1-assisted search order and explicit evidence/threshold policies without permitting a forced fallback match.
- [ ] Distinguish per-call timeouts, optional partial-result deadlines, and hard run cancellation; reserve time after extraction for metadata resolution and selection of completed content.
- [ ] Resolve one settings snapshot per run for production and benchmarks; document defaults and feature gates, and log effective settings without secrets.

### **AppContracts** ([Core/AppContracts.cs](Core/AppContracts.cs)): 
Shared data types and interfaces, grouped in one file rather than necessarily one class. Describes model messages, tool calls, run context, plan steps, product-independent extracted content, scoped requirements, metadata-resolution results, candidate evaluations, rankings, and review flags. Carries source references, units, errors, timings, and usage counts across stages without exposing host-specific storage or product-engine objects to the model.

Implementation checklist:

- [ ] Define typed chat requests, responses, tool definitions, and tool-call IDs, including finish reason, errors, request counts, and token usage.
- [ ] Define product-independent content blocks for headings, prose, lists, tables, cells, and figure descriptions; retain raw text, cleaned text, printed labels, and unit text without requiring product IDs or engine codes.
- [ ] Identify source entities by resource, page or sheet, section, table, and row; retain chunk and crop references separately from printed marks.
- [ ] Define source-coverage manifests and entity-scope closure results; distinguish unread regions, uncertain readings, missing references, and established non-applicability.
- [ ] Track per-fact and interpretation assurance, witness type, source location/revision, and reviewer or authoritative-input provenance; separate criteria compliance from source-verified recommendation eligibility.
- [ ] Define scoped requirements with source labels, values, unit text, comparison operators, mandatory/preferred status, applicability, quantities, and unresolved conflicts; preserve their links to original content.
- [ ] Distinguish independently applicable criteria from coupled constraints; retain affected entity IDs and explicit unsupported/unknown outcomes rather than manufacturing per-item allocations.
- [ ] Publish immutable criteria snapshots with revision IDs; distinguish them from mutable selection assignments and identify the baseline used in every compliance evaluation.
- [ ] Define metadata-resolution results linking source entities to detected identifiers, fetched candidate IDs, metadata versions, evidence, and unresolved or conflicting matches; keep model hypotheses separate from fetched facts.
- [ ] Record search-hint versus hard-filter roles, originating criterion or authorization rule, scope, assurance, and exclusion justification; never equate catalog existence with correct source identity.
- [ ] Define candidate evaluations with separate product-rule validity and per-requirement pass/fail/unknown results; define rankings with criteria, weights, scores, ties, and explanations.
- [ ] Define host evaluation requests/results with configuration, metric IDs, operating conditions, units, provider/data/model versions, provenance, and validity limits; include them in artifact dependencies and cache identity.
- [ ] Distinguish catalog candidates from concrete selection configurations; represent lookup/configuration search coverage, continuation state, exclusions, and stop reasons.
- [ ] Define shared decision outcomes, a reserved abstention identity, and evidence/review request and response shapes under the pipeline rules; never use a catalog ID to represent abstention.
- [ ] Define revisioned artifact dependencies, operation intent/idempotency keys, budget usage, and resume checkpoints; exclude secrets and live engine handles from retained checkpoints.
- [ ] Define generation tokens and conditional-publication results distinguishing staged, current, superseded, and indeterminate artifacts; provide the host publication contract for shared or persisted runs.
- [ ] Preserve original content separately from proposed bindings and final applied inputs; record target field IDs and reasons for defaults, inference, translation, reconciliation, or removal only in downstream results.
- [ ] Separate run termination, partial coverage, product-rule validity, and review status; distinguish hard review flags from informational notes and unverified evidence.
- [ ] Define injectable client, metadata, rule, evaluation, resource, publication, and progress interfaces; keep host objects out of model-facing JSON and expose safe snapshots of retained results.

### **CloudInference** ([Core/CloudInference.cs](Core/CloudInference.cs)): 
Client for text and vision LLM requests, following Agentic's inference-provider pattern. Sends conversation messages, document text or images, tool definitions, and structured-output requirements to the configured endpoint. Returns text, parsed tool calls, token usage, and explicit failure or truncation information; handles cancellation and timeouts without deciding whether a product selection is valid.

Implementation checklist:

- [ ] Use AppSettings' inference Model ID, API URL, and API Key for text and image requests; normalize API-base and full completion URLs and handle the configured provider's transport format.
- [ ] Preserve message roles, tool-call correlation, image payloads, and JSON schemas; gate vision, structured output, and thinking controls by actual provider support.
- [ ] Parse both text and tool responses strictly; retain finish reasons and reject malformed tool names, argument shapes, and incomplete tool-call payloads.
- [ ] Bound HTTP waits and propagate cancellation and transport failures explicitly; leave extraction retries to MediaImport's shared budget.
- [ ] Enforce input-context sizing and reserve usage through the shared run ledger before each dispatch; account for text and images and retain unknown-usage reservations.
- [ ] Respect injected HTTP-client ownership; share transport and usage accounting when switching models, with concurrency-safe counters that do not mix separate runs.
- [ ] Record model, transport, duration, requests, and input/output tokens; keep raw captures opt-in and redact credentials from diagnostics.

### **CloudDecision** ([Core/CloudDecision.cs](Core/CloudDecision.cs)):
Client for SystemOne decision requests. Sends explicit state and typed questions, supports batching independent questions, and returns answers with model and usage information. Validates question shapes, allowed choices, and numeric answer ranges; reports malformed responses and service failures instead of inventing a decision.

Implementation checklist:

- [ ] Use AppSettings' decision Model ID, API URL, and API Key; send explicit state and questions keyed by nonblank IDs, supporting independent questions in one request.
- [ ] Centralize validation for `noul`, `choice`, and `score` requests so decision tools do not duplicate protocol rules.
- [ ] Require exactly one answer per requested ID with matching type; reject unknown choices, nonfinite numbers, out-of-range values, and missing model identity.
- [ ] Apply bearer authentication when supplied, a bounded request timeout, caller cancellation, and clear HTTP or response-validation errors; never substitute a default answer.
- [ ] Size decision state and batched questions against context limits, and use the same run ledger as inference; preserve provider-valid abstention choices for explicit caller translation.
- [ ] Return answer metadata and usage, log elapsed time, and dispose only owned transport resources; support injected transport for offline tests.

### **HarnessBase** ([Core/HarnessBase.cs](Core/HarnessBase.cs)): 
Shared base for the bounded agent loop, modeled on Agentic's DecisionRouter. Maintains conversation and per-run state, requests LLM responses, dispatches registered tools, and feeds tool results back into the conversation. Enforces iteration and duration limits, honors cancellation and explicit exit, and records an ordered execution trace with errors and usage. Keeps full selection objects for the host while giving the model compact summaries.

Implementation checklist:

- [ ] Validate tool names, unique registration, descriptions, and object argument schemas; snapshot definitions so callers cannot mutate an active run.
- [ ] Preserve assistant/tool message order and tool-call IDs; execute calls in returned order and treat tool outputs as data, not new instructions.
- [ ] Reject empty, failed, malformed, or token-truncated inference responses before executing tools; return recoverable argument and tool errors as explicit tool results.
- [ ] Enforce iteration and duration limits with linked cancellation; bound waits even for noncooperative clients and prevent late completions from changing finished results.
- [ ] Own the shared budget ledger and stage reservations; stop new work on exhaustion while retaining completed results and explicit unprocessed scopes.
- [ ] Deduplicate operations under the pipeline idempotency contract; track artifact lineage and invalidate stale descendants before any tool consumes or exports them.
- [ ] Own the authoritative in-memory generation boundary or delegate it to the host coordinator; serialize revision advancement with dependency checks and current-reference publication.
- [ ] Return typed awaiting states and checkpoints for evidence/review; resume through a new revision-checked execution with inherited usage and reject obsolete responses or late completions.
- [ ] Record inference and tool traces with arguments, outputs, errors, timings, and usage; retain typed extractions, metadata matches, and selections separately from model summaries.
- [ ] Distinguish completion, failure, cancellation, and iteration or duration limits; retain partial work without relabeling an interrupted run as successful.
- [ ] Route normal assistant completion and every other terminal/awaiting path through the same typed-outcome finalizer; never display model prose as an independently authoritative selection answer.
- [ ] Honor ControlExit immediately, skip later calls in the same response, and record skipped calls; do not make another inference request after exit.

### **HarnessTool** ([Core/HarnessTool.cs](Core/HarnessTool.cs)): 
Common base for every tool exposed to the harness. Defines a stable name, a concise description, an argument schema, and an asynchronous execution method receiving run context and cancellation. Keeps tool definitions reusable while storing request-specific state in the run context; distinguishes recoverable tool errors from a failed or cancelled run.

Implementation checklist:

- [ ] Expose name, description, JSON argument schema, and asynchronous execution with explicit run context and cancellation.
- [ ] Validate required fields, types, and unsupported arguments consistently; never assume a model followed the advertised schema.
- [ ] Define a recoverable tool-error contract; distinguish invalid input, unavailable resources, provider failure, and cancellation without success-shaped fallbacks.
- [ ] Keep reusable tool instances free of mutable run state; resolve resources through host authorization and return bounded summaries rather than host objects.
- [ ] Require current input artifact revisions and operation identity from the run context; return shared unresolved, budget, and search-coverage outcomes rather than ad hoc success strings.

### **ControlPlan** ([Tools/ControlPlan.cs](Tools/ControlPlan.cs)): 
Creates, lists, and updates focused work items within a run. Records each item's ID, description, relevant context, dependencies, status, and result. Rejects duplicate IDs, missing dependencies, cycles, and attempts to start dependent work too early. Like Agentic's PlanTool, tracks work but does not itself execute steps or create parallel workers.

Implementation checklist:

- [ ] Implement create, update, and list actions with IDs, descriptions, optional context, dependency IDs, status, and result.
- [ ] Validate a complete creation batch before changing state; reject blank IDs, duplicates, unknown dependencies, self-dependencies, and cycles.
- [ ] Support pending, in-progress, completed, and failed states; require dependencies to be completed before starting or completing dependent work.
- [ ] Return the current plan in stable order and keep it run-local; leave scheduling and parallel execution to the host.

### **ControlExit** ([Tools/ControlExit.cs](Tools/ControlExit.cs)): 
Explicitly ends a run when the task is complete or cannot proceed. Records a final summary, completion status, retained selection references, and any unresolved requirements or review needs. Signals the harness to stop further tool execution and inference; does not mark unfinished or invalid selections as successful merely because the loop ended.

Implementation checklist:

- [ ] Define an explicit terminal request with reason, summary, retained result IDs, and unresolved requirements; validate IDs against the current run.
- [ ] Set the run's terminal state once and signal HarnessBase to stop; prevent subsequent tools in the response from changing or exporting results.
- [ ] Preserve partial coverage, invalid selections, review flags, and unfinished plan steps independently of the requested exit status.
- [ ] Derive recommendation eligibility and search claims from retained evaluations under the pipeline rules; an exit summary cannot upgrade provisional results or clear unresolved requests.
- [ ] Treat any supplied summary as a diagnostic proposal; pass termination intent and validated result references to the common finalizer rather than bypassing it.
- [ ] Retain host-readable results and a final trace entry on exit; keep caller cancellation and deadline termination distinct from a voluntary finish.


### **CriteriaResolver** ([Tools/CriteriaResolver.cs](Tools/CriteriaResolver.cs)):
Assembles MediaImport's generic content into product entities and scoped requirements for ProductQuery and SelectionBinder. Combines relevant rows, headings, footnotes, prose, and explicit cross-document references without modifying extraction. Interprets requirement meaning and precedence while retaining source evidence and uncertainty. Does not fetch product data, assign catalog field IDs, or bind selections.

Implementation checklist:

- [ ] Group related source blocks into entities using explicit identifiers and context; preserve quantities and repeated independent units rather than merging them solely by mark.
- [ ] Associate section-wide, document-wide, and cross-document criteria with their intended entities; retain unresolved applicability instead of silently treating every statement as global.
- [ ] Mark shared budgets, aggregate targets, and cross-product dependencies as unsupported coupled constraints; preserve their scope and block affected verified recommendations rather than treating them as independent limits.
- [ ] Close evidence scope using the source-coverage manifest; block verification for potentially affected entities until unread content or uncertain applicability is resolved under the pipeline rules.
- [ ] Carry source assurance through grouping and interpretation; require independent value/qualifier/scope witnesses or recorded review for material model-derived criteria, and never promote fidelity merely because coverage is complete.
- [ ] Represent exact values, ranges, minimums, maximums, tolerances, exclusions, and alternatives as explicit constraints; preserve original wording, labels, and unit text.
- [ ] Distinguish mandatory requirements from preferences and optional information; require explicit policy or evidence before assigning importance.
- [ ] Resolve conflicting statements only under an explicit precedence policy, such as supplied revision or authority rules; retain overridden evidence and unresolved contradictions.
- [ ] Use deterministic interpretation or bounded inference/SYS1 requests through existing clients; validate source references and abstain when meaning or scope is unsupported.
- [ ] Partition large evidence sets with shared context intact, and publish an immutable criteria revision; turn interpretation gaps into typed requests with source references and expected response shapes.
- [ ] Return source-linked requirements and unresolved questions to ProductQuery; leave catalog identity, unit compatibility, field mapping, and product-rule evaluation to downstream stages.


### **DecisionTruth** ([Tools/DecisionTruth.cs](Tools/DecisionTruth.cs)): 
Wraps a focused yes-or-no question for CloudDecision using SystemOne's `noul` question type. Supplies the evidence and judgment instructions, then returns a yes-likelihood value between 0 and 1 rather than an unsupported statement of fact. Preserves uncertainty; any threshold used to trigger action must be explicit in the calling workflow.

Implementation checklist:

- [ ] Map explicit state, question ID, instructions, and optional object criteria to a `noul` request through CloudDecision.
- [ ] Return the validated finite value in `[0, 1]`, question identity, model, and usage; do not round it into a factual yes/no answer.
- [ ] Require callers to supply any action threshold; preserve evidence references and uncertainty, and propagate service failures rather than choosing yes or no.

### **DecisionOption** ([Tools/DecisionOption.cs](Tools/DecisionOption.cs)): 
Asks CloudDecision to choose from a supplied set using SystemOne's `choice` question type. Associates each allowed option key with its criteria and returns a supported match or explicit abstention, retaining confidence information when provided. Supports decisions such as choosing a candidate product family; cannot invent catalog entries or replace subsequent metadata validation.

Implementation checklist:

- [ ] Accept 1 to 255 named provider options with criteria and explicit state; reserve one non-catalog abstention key when a match is not guaranteed, leaving at most 254 real choices in that case.
- [ ] Validate returned keys against the request; translate the reserved key into an unresolved outcome, never a bound value or proven no-match result, and preserve question identity, model, usage, and reported confidence.
- [ ] Apply the caller's declared evidence and threshold policy; absent confidence is not confidence of one, and provider failure is not an abstention or permission to choose a default.
- [ ] Use host-authorized catalog candidates for detection and fetched fields, options, or supported actions for binding decisions; retain the choice as judgment evidence, not proof of compatibility.

### **DecisionScore** ([Tools/DecisionScore.cs](Tools/DecisionScore.cs)): 
Evaluates supplied evidence against an ordered rubric using SystemOne's `score` question type. Returns a numeric position within the rubric's range, with the rubric retained so the score has a clear meaning. Supports preference or suitability comparisons, not physical calculations or a claim that product rules have passed.

Implementation checklist:

- [ ] Accept 2 to 10 ordered rubric levels, explicit state, and instructions; send a `score` question through CloudDecision.
- [ ] Accept finite scores from `0` through `level count - 1`, including fractional values; preserve rubric order, question identity, model, and usage.
- [ ] Compare candidates only under the same rubric and evidence scope; leave ranking policy to the caller and product validity to SelectionBinder.

### **MediaImport** ([Tools/MediaImport.cs](Tools/MediaImport.cs)): 
Handles ingestion and LLM extraction for host-authorized PDF, image, and spreadsheet resources. Recovers as much readable content as possible and returns it cleaned and structured, including prose, headings, lists, tables, labels, and figure descriptions. Needs no product data of any kind and does not identify catalog families or bind fields. Preserves source order, printed values, unit text, and missing or conflicting evidence; passes the resulting content to ProductQuery.

Implementation checklist:

- [ ] Resolve an opaque host-authorized resource ID to a stream; validate format support, honor cancellation, and dispose transferred resources without accepting model-supplied paths.
- [ ] Depend only on media readers, the LLM client, and generic extraction settings; require no product catalog, family registry, glossary, product rules, or product-specific prompts.
- [ ] Reconstruct PDF and spreadsheet tables with merged and repeated headers, group labels, units, and correct wrapped-cell reading order; preserve source cells before prompting.
- [ ] Route readable text, garbled text, scanned pages, and embedded image regions appropriately; orient and size images for legibility and retain page/crop relationships.
- [ ] Extract all recoverable content through the LLM, including nonschedule tables and specification prose; preserve document structure without inventing equipment rows or interpreting product relevance.
- [ ] Clean whitespace, reading order, and extraction artifacts while retaining original text; keep transcribed content distinct from inferred figure descriptions and uncertain readings.
- [ ] Chunk content with its section titles and table headers; share one bounded model-call gate across chunks, images, and retries, and isolate non-thread-safe page readers.
- [ ] Parse fenced or prose-wrapped JSON and recover only complete content blocks or rows from truncated extraction; preserve source links and report recovery as partial.
- [ ] Retry observable missing content, confirmed truncation, or empty reads of visibly populated regions within an explicit budget; merge recovered work without replacing completed evidence.
- [ ] Merge overlapping reads using source identity and multiplicity; preserve independent repeated rows and marks, and flag conflicting crop values rather than choosing silently.
- [ ] Compare source regions and cells with structured output; record missing text, missing cells, shifted headers, and uncertain transcriptions without catalog-based field or unit checks.
- [ ] Preserve original text/cell witnesses and image-region references for independent downstream checks; label model-only transcription and figure interpretation as model-interpreted, not source-confirmed.
- [ ] Emit a generic expected/read/unread/uncertain region manifest, including failed and skipped chunks; leave product applicability and evidence-scope closure to CriteriaResolver.
- [ ] Return completed sources at an opted-in soft deadline with unread-region notes; keep hard cancellation distinct, and assemble completed content in source order.
- [ ] Report source completion, stage timings, retry reasons, call/token counts, and merge counts; retain raw responses only when diagnostic capture is enabled.

### **MediaExport** ([Tools/MediaExport.cs](Tools/MediaExport.cs)): 
Turns retained selection results into a host-requested report or structured export. Includes selected products, applied requirements, source references, validation results, and review notes, clearly distinguishing complete, partial, and failed outcomes. Uses a host-authorized output destination and reports write failures; does not silently repair selections, discard flagged rows, or save changes to a product job as a side effect.

Implementation checklist:

- [ ] Resolve run-owned result IDs and a host-authorized destination; start with a defined structured export contract rather than arbitrary model-generated files.
- [ ] Reject superseded inputs and apply the pipeline's host-writer idempotency contract; retain indeterminate write status until the host confirms completion or failure.
- [ ] Serialize final applied fields alongside original evidence, units, validation diagnostics, review flags, failures, partial-result notes, and ranked alternatives with their reasons; preserve source identity and distinguish source order from rank order.
- [ ] Honor cancellation and surface serialization or write errors; report an artifact as available only after the host confirms the write completed.
- [ ] Distinguish staged bytes from current publication; publish the artifact reference only after the generation/freshness check commits, and report a racing revision change as superseded.
- [ ] Label provisional results and limited-search rankings explicitly; include coverage, criteria/metadata revisions, and unresolved requests without claiming global optimality or verified compliance.
- [ ] Export criteria-compliant versus source-verified assurance, evaluator conditions/provenance, and unsupported coupled constraints; never label a collection jointly compliant or optimized.
- [ ] Return a compact artifact reference and outcome counts; keep raw product-engine objects out of exports and never persist or approve product-job changes implicitly.

### **ProductQuery** ([Tools/ProductQuery.cs](Tools/ProductQuery.cs)): 
Receives MediaImport's cleaned, structured content and calls CriteriaResolver to assemble scoped requirements. Uses SYS1, an LLM, or both to detect product-related identifiers and search hints, then fetches the product families, fields, options, units, and rules needed by SelectionBinder through host-authorized catalog access. Keeps interpreted requirements and detected hints separate from authoritative records, preserving ambiguous or unmatched content rather than fabricating metadata. Does not alter extraction or create selections.

Implementation checklist:

- [ ] Accept retained extraction IDs and inspect full structured content with its source context; do not rely on a lossy model-generated summary.
- [ ] Invoke CriteriaResolver with source content, user-supplied requirements, and explicit scope/precedence policy; carry unresolved applicability and conflicts into lookup results.
- [ ] Use CloudDecision and/or CloudInference to detect manufacturers, product types, families, model numbers, and other search hints; attach source evidence and uncertainty to each hypothesis.
- [ ] Search only host-authorized catalogs; use detected hints for ordering or focused lookups, and validate records without treating their existence as proof of correct source interpretation.
- [ ] Apply hard filters only under the pipeline's evidence/authorization/proof rules and retain their provenance; broaden unsupported hint-narrowed searches or mark them limited before making absence claims.
- [ ] Page catalog searches and project relevant metadata for bounded model requests; retain full host-side records and preserve continuation state, filters, and lookup completeness.
- [ ] Fetch the metadata needed for binding and reconciliation: field and option IDs, codes, labels, aliases, units, input/output roles, defaults, model-number mappings, and applicable rules or constraints.
- [ ] Resolve candidates per source entity, including mixed-product documents; retain family-specific meanings and report conflicting, ambiguous, or unmatched evidence instead of silently choosing a family.
- [ ] Cache immutable fetched metadata and derived lookup data within the injected service; track versions, advance affected run generations on reload, and distinguish no match from lookup failure.
- [ ] Publish versioned catalog candidates and a consistent metadata/rule snapshot; distinguish an exhausted filtered search from a limited search with no verified match.
- [ ] Turn unresolved product identity or missing source evidence into typed requests; preserve abstentions and provider errors without manufacturing catalog identifiers.
- [ ] Return scoped requirements, source-linked candidate records, and full fetched metadata for SelectionBinder, plus bounded model summaries; keep extraction unchanged and request further evidence when lookup remains unresolved.

### **SelectionBinder** ([Tools/SelectionBinder.cs](Tools/SelectionBinder.cs)): 
Binds scoped requirements to candidate product metadata supplied by ProductQuery. Supports deterministic or SYS1-based field mapping, option selection, and reconciliation, drawing on Agentic's binding and validation safeguards. Converts compatible units, applies supported defaults, and evaluates each candidate against product rules and mandatory requirements before delegating feasible-alternative ordering to SelectionRanker. Returns final applied values, decision evidence, validation diagnostics, and ranked alternatives while keeping original content and every consequential change visible; leaves persistence and user approval to the host.

Implementation checklist:

- [ ] Consume scoped requirements and source-linked metadata-resolution results from ProductQuery; validate candidate IDs and metadata versions, and request further querying or review for unresolved product identity.
- [ ] Keep the resolved criteria snapshot immutable and bind separate mutable assignments for each catalog candidate; map source labels and values to fetched fields and options, flagging duplicate or ambiguous matches.
- [ ] Own bounded, rule-guided configuration search with stable configuration IDs, visited assignments, and sound pruning; explore relevant alternative option/value assignments rather than rejecting a product after one failed configuration.
- [ ] Record complete, limited, or unknown configuration-search coverage and continuation state; unsearched or unsupported value domains cannot establish product infeasibility or global optimality.
- [ ] Supply SYS1 with relevant source evidence, fetched metadata, and bounded candidate choices; validate returned field IDs, option IDs, and reconciliation actions before applying them.
- [ ] Parse values and compatible units through product metadata; preserve option separators and compound codes, and never bind computed-output fields as inputs.
- [ ] Check header/unit-family conflicts, minimum versus design semantics, and inconsistent numeric pairs; flag weaker evidence without presenting an inconclusive check as verification.
- [ ] Create an isolated default selection, apply bound inputs, and fill absent model-number-derived options only when metadata supports them; never overwrite printed requirements silently.
- [ ] Run host product rules and bounded deterministic or SYS1-based reconciliation on selection inputs only; never alter criteria, and revalidate and record every removal, translation, inference, or default.
- [ ] Obtain required calculated metrics from the host evaluation provider using explicit operating conditions and versioned inputs; preserve provenance and validity limits, and leave unsupported or unavailable results unknown rather than using SYS1 as a numeric substitute.
- [ ] Capture validity before and after reconciliation, then rebuild the draft from final applied inputs; keep wrong, missing, and changed printed values visible in review.
- [ ] Evaluate final selections against every applicable mandatory criterion in the original resolved snapshot, including operators, alternatives, and tolerances; missing evidence or unresolved scope remains unknown regardless of product-rule validity.
- [ ] Report criteria compliance separately from source assurance; keep coupled constraints unknown and never aggregate independent results into a whole-set compliance claim.
- [ ] Pass evaluated configurations, criteria revision, evidence closure, search coverage, and explicit preferences to SelectionRanker; preserve all rejected and unresolved alternatives with their reasons.
- [ ] Raise typed requests when identity, interpretation, or evidence blocks evaluation; re-enter the appropriate stage after a host-authorized revision rather than weakening constraints during reconciliation.
- [ ] Route invalid results, hard flags, family uncertainty, missing cells, and read conflicts to review; keep informational notes separate from blocking reasons.
- [ ] Bound CPU work and SYS1 calls separately with isolated selection state; retain source order, decision traces, and per-entity errors, with serial/parallel equivalence for deterministic work or captured decision replay.


### **SelectionRanker** ([Tools/SelectionRanker.cs](Tools/SelectionRanker.cs)):
Orders candidate selections already bound and evaluated by SelectionBinder. Separates mandatory feasibility from preferences, applies explicit ranking criteria, and explains why one feasible alternative ranks above another. Does not search catalogs, bind fields, reconcile inputs, or establish product validity. Returns no recommendation when no candidate has demonstrated mandatory compliance.

Implementation checklist:

- [ ] Consume evaluated configurations, the immutable criteria revision, evidence closure, lookup/configuration search coverage, and ranking policy; compare alternatives for the same entity and requirement scope.
- [ ] Apply the pipeline eligibility rule before scoring; failed, unknown, incomplete, stale, invalid, or hard-review-blocked configurations remain outside verified recommendations.
- [ ] Rank only per-entity alternatives under compatible evaluation conditions and units; preserve material-evidence assurance and evaluator versions, and never infer shared-budget or compatibility satisfaction across entities.
- [ ] Apply declared preference weights, score directions, and normalization rules; reject unsupported or incomparable criteria instead of inventing missing cost or performance values.
- [ ] Prefer deterministic scoring where values support it; use bounded SYS1 judgments only for declared qualitative criteria with shared rubrics and retained evidence.
- [ ] Never let preference scores or model confidence override mandatory failures, unknown compliance, or product-rule invalidity.
- [ ] Preserve ties and alternatives with stable ordering; explain criterion scores, trade-offs, missing evidence, and any policy-based tie-breaker.
- [ ] Return scoped rankings with rejected/unresolved alternatives; use no-feasible-candidate only for a fully resolved, exhausted or soundly excluded search scope, otherwise report no-verified-candidate and its limiting reason.
- [ ] Label feasible results from limited search as best among evaluated configurations; preserve continuation state and never claim a global optimum without complete search or a sound proof.


### **TestAssembly** ([../Decisive.Tests/TestAssembly.cs](../Decisive.Tests/TestAssembly.cs)): 
Shared test-assembly initialization and cleanup. Establishes isolated fixtures, fake model clients, and sample product metadata for offline tests, with explicit setup for any separately selected live checks. Ensures ordinary discovery and setup do not contact remote services and that test resources are released after execution.

Implementation checklist:

- [ ] Provide deterministic fake clients, metadata/rule fixtures, and authorized in-memory resources; allow extraction fixtures with no product services and keep discovery and initialization offline.
- [ ] Resolve fixtures independently of the current directory and isolate temporary outputs, logs, caches, and mutable state between tests and concurrent runs.
- [ ] Separate fast specification tests from explicitly selected live benchmarks; check live dependencies and apply timeouts only in the live path.
- [ ] Dispose owned clients, streams, and temporary artifacts; verify project compile inclusion, test discovery counts, and category filters after moves or renames.

### **TestSpecification** ([../Decisive.Tests/TestSpecification.cs](../Decisive.Tests/TestSpecification.cs)): 
Fast, deterministic behavior tests for the public contracts, harness, and tools. Uses fixtures and fakes to verify product-independent extraction, model-assisted metadata resolution, deterministic and SYS1-based binding, source tracking, planning, exit, cancellation, and failure reporting. Checks that missing data and reconciliation changes remain visible even when a resulting selection passes product rules.

Implementation checklist:

- [ ] Test real serialized client payloads and response parsing with fake transports, including provider-specific formats, malformed answers, and every decision-type boundary.
- [ ] Exercise tool-call correlation, recoverable errors, cyclic plans, dependency updates, iteration limits, deadlines, and exit within a multi-tool response.
- [ ] Verify cancellation against cooperative and noncooperative dependencies, ignored late results, independent concurrent runs, and actual request/concurrency counts.
- [ ] Prove MediaImport works with no product data or product-service calls; retain prose, nonschedule tables, unfamiliar product names, and other recoverable content without catalog-specific schemas.
- [ ] Cover table geometry, mixed text/image sources, truncated-content recovery, retry budgets, repeated marks, crop conflicts, missing content, and partial-result notes using small fixtures.
- [ ] Test SYS1 and LLM metadata detection, authoritative fetches, mixed-product matches, fabricated identifiers, ambiguity, no-match results, and lookup failures without changing extraction.
- [ ] Test CriteriaResolver on section scope, footnotes, cross-document references, quantities, repeated units, operators, preferences, and precedence conflicts; require source evidence and explicit unresolved outcomes.
- [ ] Replay captured extraction and metadata into deterministic and fake-SYS1 binding tests for aliases, duplicate keys, units, defaults, rule failures, reconciliation losses, and review routing.
- [ ] Reject SYS1 mappings or reconciliation actions outside fetched metadata; verify full typed extraction-to-query-to-selection handoffs, decision budgets, and preservation of original evidence.
- [ ] Test SelectionRanker on mandatory failures, unknown compliance, invalid products, weighted preferences, missing scores, qualitative decision replay, ties, and no feasible candidates; assert ranking never mutates selections.
- [ ] Test partial and unknown evidence scope, genuinely unaffected entities, and reviewer-established non-applicability; unread potentially relevant content must block verified recommendations.
- [ ] Test a product whose first configuration fails but another passes, bounded/unsupported search domains, and limited catalog pages; distinguish no verified candidate from proven infeasibility.
- [ ] Assert reconciliation cannot mutate the criteria snapshot or improve compliance by deleting a criterion; only an authorized new revision may change the baseline.
- [ ] Test abstention-key limits, unsupported choices, missing confidence, and provider failures through both tool and direct client callers; no unresolved outcome may become a fabricated match.
- [ ] Test typed evidence requests, targeted generic rereads, checkpoint resume, stale/duplicate responses, and approval without supporting evidence; retain consumed budgets across resumed executions.
- [ ] Test oversized downstream contexts, preserved shared criteria, concurrent budget reservations, retry accounting, and unknown token usage; exhaustion must retain work without claiming completion.
- [ ] Test concurrent duplicate operations, changed criteria/metadata/policies, transitive invalidation, unrelated-result reuse, and idempotent or indeterminate host writes.
- [ ] Test fully processed but mistranscribed media and incorrect qualifiers/scopes; criteria compliance cannot become source verification through confidence, model agreement, or catalog existence.
- [ ] Test a hallucinated identifier that names a real product; hint-directed lookup must not exclude other authorized candidates without a justified hard filter.
- [ ] Test unsupported recommendation claims in normal model completion, ControlExit, and limit/awaiting paths; only current typed outcomes may populate the public final answer.
- [ ] Race revision changes against staged export writes and final-response publication; conditional commit must reject stale publication, while changes after commit mark the prior result historical.
- [ ] Test fake host evaluation results under differing conditions/units, missing inputs, provider errors, unsupported metrics, and changed evaluator/data versions; unknown numeric results must never become zero, model guesses, or a pass.
- [ ] Test independently valid products that violate a shared total or compatibility condition; report unsupported coupled constraints and block whole-set claims without inventing per-item allocations.
- [ ] Assert final draft fidelity and serial/parallel equivalence, not validity alone; cover authorized resource access, export failures, and absence of implicit job persistence.
- [ ] Keep each test within a few seconds with no network, live model, or database dependency; inject clocks or controlled tasks instead of long sleeps.

### **TestBenchmark** ([../Decisive.Tests/TestBenchmark.cs](../Decisive.Tests/TestBenchmark.cs)): 
Measures content extraction, metadata resolution, and selection quality as separate stages against fixtures with known expected results. Reports source coverage, product-match accuracy, field and option fidelity, review rates, validity before and after reconciliation, timings, model-call counts, and token usage. Records extraction, lookup, and binding model/configuration details so runs can be compared; keeps slow or live-service measurements explicitly selected and separate from fast specification tests.

Implementation checklist:

- [ ] Use source-verified text, tables, marks, and full-row references across PDF, image, and spreadsheet fixtures, including prose and non-product content; never assume contiguous numbering or document-wide mark uniqueness.
- [ ] Measure source-scoped text and table coverage, invented content, missing blocks, and value fidelity independently of product knowledge; compare original evidence with final applied fields without counting defaults as extracted evidence.
- [ ] Score product detection, catalog-match accuracy, metadata completeness, and unresolved-candidate reporting separately from extraction and binding; compare deterministic and SYS1 binding on identical extracted content and metadata.
- [ ] Score requirement meaning and applicability against source-verified references; measure mandatory-compliance errors and ranking consistency under fixed objectives separately from product-rule validity.
- [ ] Report evidence-scope closure and catalog/configuration search coverage alongside ranking quality; measure false verified recommendations under incomplete input and false infeasibility claims under limited search.
- [ ] Score transcription/interpretation fidelity independently of coverage and criteria compliance; report assurance levels, unjustified filter exclusions, and unsupported coupled scopes separately.
- [ ] Report validity before and after reconciliation separately; measure defective rows escaping review and clean rows unnecessarily flagged, not review rate alone.
- [ ] Record per-stage timings, requests, retries, tokens, partial coverage, extraction/query/binding model identities, backend, prompts, effective settings, and fetched metadata versions under production-equivalent configuration.
- [ ] Use captured replay to isolate downstream changes; compare live extraction changes across multiple files and repeated runs, with hold-outs reported separately and never used for tuning.
- [ ] Apply explicit per-fixture acceptance thresholds and fail on regressions or missing required reference data; keep live runs opt-in, bounded, and isolated in their artifacts.