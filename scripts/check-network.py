import ipaddress
import os

if os.environ.get("VNET_ENABLED", "false").lower() != "true":
    value = os.environ.get("ALLOWED_CLIENT_PREFIX", "")
    if not value:
        print("No external Storage client rule. Use the scheduler-native synthetic demo.")
        raise SystemExit(0)
    network = ipaddress.ip_network(value, strict=True)
    if network.prefixlen != network.max_prefixlen:
        raise SystemExit(
            "This demo requires one specific client address (/32 or /128), not a broad range."
        )
    if not network.network_address.is_global:
        raise SystemExit(
            "Use the approved client's public address, not an internal service endpoint address."
        )
print("Network configuration validated; Entra authentication remains required.")
