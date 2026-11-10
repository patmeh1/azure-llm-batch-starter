# Reusable implementation prompt

Build a domain-neutral Azure OpenAI Batch starter that a developer can download,
deploy into their own subscription, and test with synthetic records.

Use Python, the official OpenAI/Azure SDKs, Azure Durable Functions with Durable
Task Scheduler, managed identities, parameterized Bicep/azd deployment, and a
read-only operations dashboard. Reuse licensed official Microsoft framework
samples and preserve attribution; do not reinvent a durable execution engine.

Implement sequential online, paced parallel online, and actual service-managed
Batch modes. Keep the model/version, input dataset, prompt/schema, and generation
settings explicit so comparisons are interpretable. Verify regional model/SKU
availability and quota before deploying; never silently change models or regions.

Provide durable submission IDs, restart/resume, bounded retries, exact result
reconciliation, partial-failure handling, and explicit unknown/ambiguous states.
Never turn missing results or infrastructure errors into successful records.

Measure complete run duration, preparation, attempts, admission/retry waiting,
service Batch timestamps where available, records/minute, input/output/cache/
reasoning usage, observed RPM/TPM, and errors. Keep per-call latency separate
from Batch queue/turnaround time. Do not invent unavailable metrics.

Create deterministic synthetic data with configurable sizes, including a bounded
profile targeting twice a user-supplied input-token baseline. Clearly distinguish
tokenizer estimates from API-reported usage and synthetic fixtures from live runs.
Explicit, small, opt-in stress experiments may observe 429s; normal processing
must respect the service's retry guidance and application budgets.

Include local offline and real Azure demos. A scheduler-native synthetic
launcher may generate input inside Azure when direct client Storage access is
restricted. Keep raw documents in private Storage; return operational metadata
through the authorized control plane. Do not bypass governance or create
policy-exclusion tags to make a demo work.

Include dashboard source, architecture diagrams, a step-by-step tutorial,
packaging, CI, timed unit/integration/browser tests, fault-injection tests, and
sanitized real-run evidence. Verify fresh-code packaging, not merely that a ZIP
exists. Report pending live work as pending.

Ask the operator to confirm subscription, location, data-residency boundaries,
spending limits, and retention. Use resource-scoped identities and guarded
cleanup. Keep secrets, actual accounts, private endpoints, real datasets, and
internal correspondence out of public Git history and artifacts.

Do not promise zero 429s, unlimited scale, guaranteed speedups, or a Batch
completion deadline that the service does not guarantee.

## V2 extension

Preserve the simple baseline and add an opt-in complex synthetic workload.
Keep the actual system instructions, active hierarchical taxonomy/examples,
and user instructions in readable Markdown assets included in source, wheel,
and deployed worker packages. Reproduce structural challenges using independent
fictional domains, not customer wording, policies, or records.

Generate deterministic conversations with corrections, negation, menu noise,
stale prior classification, meaningful secondary requests, partial evidence,
unresolved handoffs, confidence preservation, and quoted instruction-like data.
Keep gold labels outside model messages. Check eleven output fields, rule
constraints, and identifier canaries; export a twelve-column analysis including
the application-owned record ID.

Add cost-bounded same-data sequential/parallel/cloud-Batch load scenarios that
preview by default. Persist run IDs before paid submission, preserve pending
work, and use client/worker prompt-bundle hashes to reject mismatches. Report
schema-valid processing separately from semantic quality. Publish measured
quality failures without altering expected answers to make the experiment pass.

## V3 extension

Keep v2 prompts/schema immutable and add a separate GPT-5 mini US
DataZoneStandard deployment without replacing existing models. Verify exact
regional SKU availability; pricing meters alone do not prove deployability.
If service Batch is unavailable, obtain operator approval before using a
clearly labeled client-side grouped comparison.

Compare 1,700 sequential online requests against five fixed concurrent streams
of 340 using the same synthetic inputs. Preserve bounded retries, original
deadlines, conservative cost/attempt reservations, durable checkpoints, and
unknown in-flight outcomes across resume. Never blindly replay a paid online
request whose response is unknown.

Provide a guide and allowlisted output exports. Keep live results explicitly
pending until both modes are terminal. Report test assumptions and measured
limits without claiming exact reproduction of an unknown production system.

## V4 extension

Test whether multiple GPT-5 mini US DataZoneStandard deployments help when
aggregate quota is held constant. Retain the single 150K TPM / 150 RPM
control and add three 50K TPM / 50 RPM deployments in the same approved
account, subscription, and region. Do not include the control in the pool,
increase quota, buy PTUs, or change geography silently.

Run both arms in Azure Durable Functions/Scheduler with five ordered
workers, identical synthetic complex-v2 prompts/records, and the same
generation/routing/retry settings. Use round robin among eligible targets,
shared per-target cooldowns, service retry headers first, and a 30-second
exponential fallback with jitter. Disable SDK retries and allow at most
four total attempts per record across targets. Clearly label this as an
online throttling observation profile, not service Batch or a proactive
token-paced production configuration.

Start with an operator-approved 65-record-per-arm pilot. Provide a dry-run
1,700-record scenario without executing it unless separately approved.
Preserve attempt/cost/deadline bounds, atomic paid intents/responses,
explicit ambiguous outcomes, cancellation checks, and an exclusive routing
lease that remains held when reconciliation is uncertain.

Publish measured failures, per-target limits/429s, record and HTTP latency,
router waits, token usage, unchanged semantic gates, and inspected synthetic
examples. Update the comparison site with a separate v4 page and fourth
column, while retaining v3's interrupted status. Deployment count alone
does not add quota; HTTP 429 alone does not reveal internal scale-up state.
