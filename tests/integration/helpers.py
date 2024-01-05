# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import yaml
from ops.model import ActiveStatus, BlockedStatus
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
POSTGRESQL_NAME = "postgresql-k8s"
POSTGRESQL_CHANNEL = "14/stable"
NGINX_INGRESS_CHARM_NAME = "nginx-ingress-integrator"
ACTIVE_STATUS = ActiveStatus.name
BLOCKED_STATUS = BlockedStatus.name


async def get_unit_url(ops_test: OpsTest, application, unit, port, protocol="http"):
    """Returns unit URL from the model.

    Args:
        ops_test: PyTest object.
        application: Name of the application.
        unit: Number of the unit.
        port: Port number of the URL.
        protocol: Transfer protocol (default: http).

    Returns:
        Unit URL of the form {protocol}://{address}:{port}
    """
    # Sometimes get_unit_address returns a None, no clue why, so looping until it's not
    url = await ops_test.model.applications[application].units[unit].get_public_address()
    return f"{protocol}://{url}:{port}"


async def perform_livepatch_integrations(ops_test: OpsTest):
    """Add relations between Livepatch charm, postgresql-k8s, and nginx-ingress.

    Args:
        ops_test: PyTest object.
    """
    logger.info("Integrating Livepatch and Postgresql")
    await ops_test.model.integrate(f"{APP_NAME}:database", f"{POSTGRESQL_NAME}:database")
    await ops_test.model.integrate(f"{APP_NAME}:ingress", f"{NGINX_INGRESS_CHARM_NAME}:ingress")

    def checker():
        return (
            "Waiting for postgres relation"
            not in ops_test.model.applications[APP_NAME].units[0].workload_status_message
        )

    await ops_test.model.block_until(checker)
