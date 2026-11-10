# Microsoft framework reuse

This application uses **Azure Durable Functions** (Python v2 programming model)
with **Durable Task Scheduler**, Microsoft's managed durable execution backend.
It does not implement its own durable execution engine.

The default synthetic launcher uses the official `durabletask-azuremanaged`
Python client to schedule `synthetic_workflow` in the same task hub as the
Durable Functions workers. Data generation happens in an authorized activity
inside Azure. This does not open private Storage or proxy its documents through
the client; only operational metadata is returned through scheduler history.
Custom-file transport uses Storage/Queue and requires approved connectivity.
The v2 `complex_workflow` uses the same backend but verifies the client's
Markdown/schema bundle hash and expected model/version before creating its
synthetic manifest. Expected answers stay in private artifacts, not inference
messages.

## Upstream foundations

- [Deployable Python Durable Functions quickstart](https://github.com/Azure-Samples/durable-functions-quickstart-python-azd),
  pinned revision `9253c446df12494148c754ae1d825fd58265c8d0`.
- [Durable Task Scheduler samples](https://github.com/Azure-Samples/Durable-Task-Scheduler),
  pinned revision `7152d82c656576b9cda47d33ac2de4f61aacff66`.
- [Official scheduler quickstart](https://learn.microsoft.com/azure/durable-task/scheduler/quickstart-durable-task-scheduler).

The MIT notice is preserved in `LICENSE.upstream`. Application-specific work
includes the Azure OpenAI adapter, Batch lifecycle activities, quota reservations,
reconciliation, timing model, and dashboard. The upstream sample by itself is not
an Azure OpenAI Batch solution.

## Framework features reused

- Durable activity history, replay, sub-orchestrations, timers, and entities.
- Managed scheduler state and an authenticated native operations dashboard.
- Official azd/Bicep Flex Consumption foundation, managed-identity connection
  settings, Storage data roles, and monitoring.

The application adds `prepare_run`, `step_partition`, `finalize_run`, and
`cancel_partition` activities. `batch_budget` serializes reservations across
partitions in the task hub. Upload/submission outcomes are stored before later
steps; unknown submissions are not blindly repeated.

Activities and queue messages can be delivered more than once. **Exactly-once
remote inference is not guaranteed.** Stable IDs, persisted remote IDs, and
explicit ambiguous states limit duplicate submissions and make recovery
auditable.

## Runtime configuration

The Functions `host.json` selects the `azureManaged` scheduler provider and an
extension bundle supporting it. The scheduler connection uses managed identity
and the deployed task hub; it contains no account key.

The package uses the declared SDK ranges and the generated dependency lock.
See [testing](testing.md) for versions actually exercised.

The service-level `prepackage` hook copies the current shared library and
Markdown prompts into the Functions package and checks their hashes. Windows
and POSIX wrappers invoke the same Python packager. This hook also runs during
`azd deploy`; merely copying the library once can leave new function entry
points calling stale helper code.

## Monitoring versus business completion

A Durable instance can finish its orchestration with an application result of
`needs_attention`. Always inspect that result and input/output reconciliation,
not only the framework's `Completed` status.

If a submission is ambiguous, its quota reservation stays held until the remote
outcome can be established. This favors avoiding duplicate paid jobs over
silently maximizing throughput. Operators must inspect the saved job and remote
Batch metadata before releasing or replacing it.

Queued partitions check cancellation before each admission attempt. A cancelled
partition that has never been submitted does not need to wait for capacity.
Ambiguous submissions retain their reservation until an operator establishes
the remote outcome; cancellation does not make an unknown paid submission safe
to repeat.

The host and starters explicitly use orchestration version `5.0`.
The separately named V5 shared-queue workflows are new; prior workflow
sequences remain compatible with versions `1.0`, `2.0`, `3.0`, and `4.0`.
Sub-orchestrations inherit the root version. `CurrentOrOlder` matching with
`Reject` prevents an older worker from accepting newer-version work during
rolling deployment. The original unversioned activity sequence is retained
for recovery of early development runs.

For future control-flow changes, increment the worker/client version together
and preserve each older version's activity sequence. Draining existing runs
alone is insufficient: old workers can briefly remain alive after a successful
deployment and accept newly submitted unversioned work. See Microsoft's
[orchestration versioning guidance](https://learn.microsoft.com/azure/durable-task/common/durable-orchestration-versioning).

An orchestration failure does not stop a previously submitted Batch job.
Before recovery, inspect its history and saved submission identity. Restore
compatible code before using the scheduler's rewind operation; do not start a
replacement paid job or release its reservation while its remote outcome is
unknown.

If incompatible events have already entered a failed orchestration's history,
rewind can still fail. After investigating the failure, fixing its cause, and
confirming its old activities are no longer running, use:

```powershell
llm-batch azure-recover FAILED_ROOT_ID --config .azure\demo-config.json
```

Recovery uses fresh orchestration/child histories but the **same persisted
manifest, partition metadata, remote Batch IDs, and admission keys**. It does
not regenerate or blindly resubmit inference. An ambiguous saved submission
still requires operator reconciliation. Repeating recovery for the same failed
root returns the same recovery ID. The original failed history remains visible,
and reporting includes the prior application's elapsed time instead of claiming
the recovery observer processed the entire dataset in seconds.

## V4 online execution and recovery boundary

`multi_deployment_workflow` runs the single and pool arms serially;
`multi_deployment_group` owns an ordered subset of records. The
`multi_router` entity provides an exclusive experiment lease, bounded
attempt/cost reservations, round robin among non-cooling targets, and a
non-resetting deadline. Model calls remain in `v4_activity`, never in
replayed orchestration code. Timers wait without holding an invocation.

Atomic private Blob intent/response pairs protect paid online attempts.
An intent without a saved response, or a connection/timeout with uncertain
completion, is recorded as `ambiguous_online_result` instead of blindly
retried. Such a final report retains the router lease for investigation.
Non-ambiguous terminal results release it, even when processing or semantic
gates failed. New runs must not erase a held lease.

The Batch recovery and selective-retry commands deliberately reject v4
roots. A model's semantic failure is evidence, not permission to create
another paid Batch job. See [v4 operations](v4-multi-deployment.md) for
monitoring, cancellation, private artifacts, and the larger unexecuted
scenario.

## V5 admission and completion clocks

V5 shares the existing global online experiment lease and uses a separate
per-stage admission entity for its queue, idempotent grants/acknowledgements,
request/token budgets, cooldowns, and immutable deadline. Root and child
histories remain model-payload-free. Non-paid metadata steps use bounded
activity retries; inference keeps atomic saved intent/response reconciliation.

Each arm has five warm-ups followed by a 65-second pause before the benchmark.
A failed warm-up requests cancellation rather than silently testing fewer
working endpoints. Warm-up counts/costs are separate from the 520 main
records. The reactive and adaptive arms use the same caching and shared queue.

The original stage-start timestamp controls admission, including after a
metadata activity retry. A saved paid response reconciles even after deadline
or cancellation; an uncertain intent remains explicit. Record timing starts
at first readiness at the queue head, while backlog-inclusive lifetime
starts at arm enqueue. HTTP attempt duration is recorded separately.
See [the V5 guide](v5-adaptive.md) for precise definitions and safe monitoring.
