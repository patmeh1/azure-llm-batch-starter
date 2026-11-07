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

The service-level `prepackage` hook copies the current shared library into the
Functions package and checks every module hash. This hook also runs during
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

The host and both starters explicitly use orchestration version `2.0`.
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
