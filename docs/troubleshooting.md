# Troubleshooting

| Symptom | Check/action |
|---|---|
| More parallel workers, no throughput gain | Compare shared TPM/RPM, input size, attempts, and retry time |
| HTTP 429 | Inspect sanitized error/limit headers; distinguish rate exhaustion from capacity; honor retry delay |
| One request exceeds local TPM | Increase configured allocation only after confirming Azure quota, reduce input, or use Batch |
| Model exists but Batch fails | Verify the exact version lists `DataZoneBatch`; online support is not Batch support |
| Authentication uses the wrong account | Check `az account show`; explicitly select the intended tenant/subscription |
| 403 after deployment | Check resource-scoped roles and allow propagation; do not enable shared keys |
| File upload/job creation denied | Batch needs file/job writes; inference-only OpenAI User permissions are insufficient |
| Function bindings missing | Inspect deployment package and host/SDK versions; shared application must be included |
| Ambiguous submission | Inspect saved submission key and remote job metadata before any resubmission |
| Pending after CLI timeout | Azure work continues; re-run status, do not create a replacement job |
| Durable completed, records missing | Inspect application status and reconciliation, not just runtime status |
| Missing result IDs | Preserve error files; never fabricate successful rows |
| Dashboard blank | Select the right local database or private Azure configuration; run the offline demo separately |
| Capacity/region unavailable | Stop; no automatic change to model, geography, SKU, or paid capacity |
| Account `RequestConflict` during provisioning | Serialize project/model child resources; the template creates the project after the deployments. Retry idempotently once the existing operation finishes; do not delete the account. |
| Storage stays network-disabled despite configuration | Inspect inherited management-group policies. Use the enforced perimeter or approved private networking; do not add exclusion tags to defeat governance. |
| Storage 403 with correct roles | Verify the perimeter association is Enforced and the client address or runtime subscription rule matches; network and authorization checks are separate. |

For support, collect UTC windows, deployment type/version, request IDs, sanitized
429 headers, attempt counts, and token usage. Do not send secrets or raw
customer transcripts through diagnostic logs.

The scheduler provider might need explicit registration. A private endpoint
deployment requires network reachability from the client; DNS/network failures
are not fixed by changing roles.
