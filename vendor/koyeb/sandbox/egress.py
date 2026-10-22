# coding: utf-8

"""Outbound egress policy building and destination normalization."""

from __future__ import annotations

import ipaddress
from typing import List, Optional

from koyeb.api.models.egress_policy import EgressPolicy
from koyeb.api.models.egress_policy_mode import EgressPolicyMode
from koyeb.api.models.network_policy import NetworkPolicy
from koyeb.api.models.network_policy_destination import NetworkPolicyDestination

from .errors import EgressPolicyError


def _normalize_destination(entry: str) -> str:
    """
    Normalize an outbound allowlist entry to a CIDR string.

    Bare IPv4 addresses become /32, bare IPv6 addresses become /128,
    CIDR notation is validated and passed through (host bits are cleared,
    e.g. "10.0.0.1/8" becomes "10.0.0.0/8").

    Raises:
        EgressPolicyError: If the entry is not a valid IP address or CIDR
    """
    value = entry.strip() if isinstance(entry, str) else ""
    if not value:
        raise EgressPolicyError(f"Invalid outbound_allowlist entry: {entry!r}")
    if "%" in value:
        raise EgressPolicyError(
            f"Invalid outbound_allowlist entry {entry!r}: "
            "expected an IP address or CIDR (scoped/zone-ID addresses are not allowed)"
        )
    try:
        if "/" in value:
            return str(ipaddress.ip_network(value, strict=False))
        address = ipaddress.ip_address(value)
    except ValueError as e:
        raise EgressPolicyError(
            f"Invalid outbound_allowlist entry {entry!r}: "
            "expected an IP address or CIDR"
        ) from e
    prefix = 32 if address.version == 4 else 128
    return f"{address}/{prefix}"


def build_network_policy(
    block_network: bool = False,
    outbound_allowlist: Optional[List[str]] = None,
) -> Optional[NetworkPolicy]:
    """
    Build a NetworkPolicy from sandbox network policy arguments.

    Args:
        block_network: If True, block all outbound network access
        outbound_allowlist: List of IPs/CIDRs allowed as outbound
            destinations; all other outbound traffic is blocked. Bare IPs
            are normalized to /32 (IPv4) or /128 (IPv6). An empty list
            blocks all outbound traffic.

    Returns:
        NetworkPolicy, or None when both arguments are unset
        (block_network=False and outbound_allowlist=None)

    Raises:
        EgressPolicyError: If both arguments are passed, or an allowlist
            entry is not a valid IP address or CIDR
    """
    if block_network and outbound_allowlist is not None:
        raise EgressPolicyError(
            "block_network and outbound_allowlist are mutually exclusive; "
            "pass at most one"
        )
    if not block_network and outbound_allowlist is None:
        return None

    allow_list = None
    if outbound_allowlist is not None:
        allow_list = [
            NetworkPolicyDestination(cidr=_normalize_destination(entry))
            for entry in outbound_allowlist
        ]
    return NetworkPolicy(
        egress=EgressPolicy(
            mode=EgressPolicyMode.EGRESS_POLICY_MODE_DENY_ALL,
            allow_list=allow_list,
        )
    )
