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
