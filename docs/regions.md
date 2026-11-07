# Regions and model support

The demo defaults to `southcentralus` for every region-scoped resource and uses
GPT-5.4 mini `2026-03-17` with DataZoneStandard and DataZoneBatch deployments.

Always run preflight in your subscription. A model's presence in a catalog does
not prove support for a particular SKU, available quota, or deployable capacity.
The application never silently replaces a model or selects another geography.

US Data Zone inference may execute elsewhere in the US. A South Central US
resource does not guarantee South Central US-only processing. Global Batch
has a different processing boundary and is not an implicit fallback.

Other users can explicitly adapt the location/model parameters after checking
all component availability and reviewing costs/residency. The upstream template
contains a bounded location allow-list; update it deliberately when adding a
new validated region.

Current references:

- [Batch API availability](https://learn.microsoft.com/azure/foundry/openai/how-to/batch)
- [Model availability](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability)
- [Durable Task Scheduler regions](https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler)
- [Quota management](https://learn.microsoft.com/azure/foundry/openai/how-to/quota)
