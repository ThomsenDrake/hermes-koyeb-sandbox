# coding: utf-8

"""Status classification for readiness polling, failing closed on
unknown or terminal states."""

from __future__ import annotations

from typing import Literal, Union

from koyeb.api.models.deployment_status import DeploymentStatus
from koyeb.api.models.service_status import ServiceStatus


StatusClassification = Literal["ready", "in_progress", "terminal_failure"]


def classify_service_status(status: Union[ServiceStatus, str]) -> StatusClassification:
    """Classify a service status for readiness, failing closed.

    HEALTHY and DEGRADED are usable, STARTING and RESUMING are still in
    progress, and every other state — including unknown forward-compat
    values — is a terminal failure. Mirrors the JS SDK's
    classifyServiceStatus (src/claim.ts).
    """
    if status in (ServiceStatus.HEALTHY, ServiceStatus.DEGRADED):
        return "ready"
    if status in (ServiceStatus.STARTING, ServiceStatus.RESUMING):
        return "in_progress"
    return "terminal_failure"


def classify_deployment_status(
    status: Union[DeploymentStatus, str],
) -> StatusClassification:
    """Classify a deployment status for readiness, failing closed.

    HEALTHY and DEGRADED are ready, the pre-ready states (PENDING,
    PROVISIONING, SCHEDULED, ALLOCATING, STARTING) are in progress, and every
    other state — including SLEEPING, STASHED, and unknown forward-compat
    values — is terminal: it will not become ready on its own during a wait.
    """
    if status in (DeploymentStatus.HEALTHY, DeploymentStatus.DEGRADED):
        return "ready"
    if status in (
        DeploymentStatus.PENDING,
        DeploymentStatus.PROVISIONING,
        DeploymentStatus.SCHEDULED,
        DeploymentStatus.ALLOCATING,
        DeploymentStatus.STARTING,
    ):
        return "in_progress"
    return "terminal_failure"
