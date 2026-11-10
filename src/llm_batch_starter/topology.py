from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import Settings, credential


def verify_topology(
    settings: Settings, targets: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if not settings.model_resource_id:
        raise ValueError(
            "Set model_resource_id in the private configuration before a V4/V5 live run; "
            "configured quotas must be checked, not assumed."
        )
    identity = credential(settings)
    try:
        token = identity.get_token("https://management.azure.com/.default").token
        base = "https://management.azure.com" + settings.model_resource_id

        def read(suffix: str) -> dict[str, Any]:
            request = Request(
                base + suffix + "?api-version=2025-06-01",
                headers={"Authorization": "Bearer " + token},
            )
            with urlopen(request, timeout=30) as response:
                return dict(json.load(response))

        account = read("")
        if account["location"].lower().replace(" ", "") != settings.region:
            raise ValueError("Model account region differs from the approved experiment")
        domain = account["properties"]["customSubDomainName"]
        if (urlparse(settings.endpoint).hostname or "").split(".")[0] != domain:
            raise ValueError("Model resource ID and inference endpoint refer to different accounts")
        response = read("/deployments")
        if response.get("nextLink"):
            raise ValueError(
                "Deployment listing was paginated; operator topology review is required"
            )
        deployments = {item["name"]: item for item in response["value"]}
        result = []
        policies = set()
        for alias, expected in targets.items():
            item = deployments.get(expected["deployment"])
            if item is None:
                raise ValueError(f"Required model target is missing: {alias}")
            properties, sku = item["properties"], item["sku"]
            policy = properties.get("raiPolicyName")
            if not isinstance(policy, str) or not policy:
                raise ValueError(f"Content policy metadata is unavailable for target {alias}")
            policies.add(policy)
            model = properties["model"]
            if (
                properties["provisioningState"] != "Succeeded"
                or model["name"] != "gpt-5-mini"
                or model["version"] != "2025-08-07"
                or sku["name"] != "DataZoneStandard"
                or sku["capacity"] * 1000 != expected["tpm"]
            ):
                raise ValueError(f"Target {alias} differs from its pinned model/SKU/allocation")
            result.append(
                {
                    "alias": alias,
                    "model": model["name"],
                    "version": model["version"],
                    "sku": sku["name"],
                    "allocated_tpm": expected["tpm"],
                    "allocated_rpm": expected["rpm"],
                    "resource_region": settings.region,
                }
            )
        if len(policies) != 1:
            raise ValueError("Experiment targets have different content policies")
        return result
    finally:
        identity.close()
