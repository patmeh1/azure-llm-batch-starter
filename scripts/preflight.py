from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import uuid


def az(*arguments: str):
    executable = shutil.which("az")
    if not executable:
        raise SystemExit("Install Azure CLI before running Azure preflight.")
    result = subprocess.run(
        [executable, *arguments, "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description="Read-only Azure capability/quota preflight.")
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--region", default="southcentralus")
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--version", default="2026-03-17")
    parser.add_argument("--online-capacity", type=int, default=150)
    parser.add_argument("--batch-capacity", type=int, default=10000)
    parser.add_argument(
        "--online-only",
        action="store_true",
        help="Validate only US DataZoneStandard; no Batch or orchestration support is implied.",
    )
    args = parser.parse_args()
    uuid.UUID(args.subscription)
    if not re.fullmatch(r"[a-z0-9]+", args.region):
        raise SystemExit("Use the Azure region code, such as southcentralus.")
    account = az("account", "show")
    if account["id"] != args.subscription:
        raise SystemExit(
            "The active Azure CLI subscription differs from --subscription. Select it explicitly."
        )
    catalog = az(
        "cognitiveservices",
        "model",
        "list",
        "--location",
        args.region,
        "--subscription",
        args.subscription,
    )
    models = [
        entry["model"]
        for entry in catalog
        if entry.get("kind") == "AIServices"
        and entry["model"]["name"] == args.model
        and entry["model"]["version"] == args.version
    ]
    if not models:
        raise SystemExit("The requested AI Services model/version is not listed in this region.")
    skus = {sku["name"]: sku for sku in models[0]["skus"]}
    usage = az(
        "cognitiveservices",
        "usage",
        "list",
        "--location",
        args.region,
        "--subscription",
        args.subscription,
    )
    quota = {entry["name"]["value"]: entry for entry in usage}
    checks = []
    targets = [
        ("DataZoneStandard", args.online_capacity),
        ("DataZoneBatch", args.batch_capacity),
    ]
    for kind, requested in targets[:1] if args.online_only else targets:
        if kind not in skus:
            raise SystemExit(
                f"{args.model} {args.version} does not list {kind} in {args.region}. No fallback performed."
            )
        name = skus[kind]["usageName"]
        row = quota.get(name)
        if row is None:
            raise SystemExit(f"No quota information for {kind}; obtain quota before deployment.")
        available = row["limit"] - row["currentValue"]
        checks.append(
            {"sku": kind, "available_capacity_units": available, "requested_units": requested}
        )
        if available < requested:
            raise SystemExit(f"Insufficient unallocated quota for {kind}.")
    if args.online_only:
        if models[0].get("capabilities", {}).get("area") != "US":
            raise SystemExit("V3 requires a catalog-confirmed US Data Zone region.")
        print(
            json.dumps(
                {
                    "region": args.region,
                    "model": args.model,
                    "version": args.version,
                    "quota": checks,
                    "scope": "online_only",
                    "batch_support_asserted": False,
                    "note": "Catalog/quota checks are not a capacity guarantee. No resource changes were made.",
                },
                indent=2,
            )
        )
        return
    flex = az("functionapp", "list-flexconsumption-locations", "--subscription", args.subscription)
    if args.region not in [item["name"] for item in flex]:
        raise SystemExit("Functions Flex does not list the selected region.")
    provider = az(
        "provider",
        "show",
        "--namespace",
        "Microsoft.DurableTask",
        "--subscription",
        args.subscription,
    )
    locations = next(
        item["locations"]
        for item in provider["resourceTypes"]
        if item["resourceType"] == "schedulers"
    )
    if args.region not in [name.replace(" ", "").lower() for name in locations]:
        raise SystemExit("Durable Task Scheduler does not list the selected region.")
    print(
        json.dumps(
            {
                "region": args.region,
                "model": args.model,
                "version": args.version,
                "quota": checks,
                "durable_provider_registration": provider["registrationState"],
                "note": "Catalog/quota checks are not a capacity guarantee. Review Data Zone residency and costs.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
