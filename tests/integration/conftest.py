# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.


import asyncio
import logging

import pytest
import pytest_asyncio
from charm_utils import fetch_charm
from helpers import (
    ACTIVE_STATUS,
    APP_NAME,
    BLOCKED_STATUS,
    NGINX_INGRESS_CHARM_NAME,
    POSTGRESQL_CHANNEL,
    POSTGRESQL_NAME,
    get_unit_url,
    perform_livepatch_integrations,
)
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


@pytest.mark.skip_if_deployed
@pytest_asyncio.fixture(name="deploy", scope="module")
async def app(ops_test: OpsTest):
    """Build and deploy the app and its components."""
    logger.info("Building local charm")
    charm = await fetch_charm(ops_test)
    jammy = "ubuntu@22.04"
    resources = {
        "livepatch-server-image": "localhost:32000/livepatch-server:latest",
        "livepatch-schema-upgrade-tool-image": "localhost:32000/livepatch-schema-tool:latest",
    }

    asyncio.gather(
        ops_test.model.deploy(
            charm,
            config={
                "patch-storage.type": "postgres",
                "external_hostname": "",
                "auth.basic.enabled": True,
                "contracts.enabled": False,
                "patch-cache.cache-size": 128,
                "patch-cache.cache-ttl": "1h",
                "patch-cache.enabled": True,
                "patch-sync.enabled": True,
                "server.burst-limit": 500,
                "server.concurrency-limit": 50,
                "server.is-hosted": True,
                "server.log-level": "info",
            },
            resources=resources,
            num_units=1,
            application_name=APP_NAME,
            base=jammy,
        ),
        ops_test.model.deploy(
            POSTGRESQL_NAME,
            base=jammy,
            channel=POSTGRESQL_CHANNEL,
            trust=True,
            application_name=POSTGRESQL_NAME,
        ),
        ops_test.model.deploy(NGINX_INGRESS_CHARM_NAME, trust=True, application_name=NGINX_INGRESS_CHARM_NAME),
    )

    async with ops_test.fast_forward():
        logger.info(f"Waiting for {NGINX_INGRESS_CHARM_NAME}")
        await ops_test.model.wait_for_idle(
            apps=[NGINX_INGRESS_CHARM_NAME], status="active", raise_on_blocked=False, timeout=600
        )

        logger.info(f"Waiting for {POSTGRESQL_NAME}")
        await ops_test.model.wait_for_idle(
            apps=[POSTGRESQL_NAME], status=ACTIVE_STATUS, raise_on_blocked=False, timeout=600
        )

        logger.info(f"Waiting for {APP_NAME}")
        await ops_test.model.wait_for_idle(apps=[APP_NAME], status=BLOCKED_STATUS, raise_on_blocked=False, timeout=600)

        logger.info("Setting server.url-template")
        url = await get_unit_url(ops_test, application=NGINX_INGRESS_CHARM_NAME, unit=0, port=80)
        url_template = url + "/v1/patches/{filename}"
        logger.info(f"Set server.url-template to {url_template}")
        await ops_test.model.applications[APP_NAME].set_config({"server.url-template": url_template})

        await ops_test.model.wait_for_idle(apps=[APP_NAME], status=BLOCKED_STATUS, raise_on_blocked=False, timeout=300)
        logger.info("Check for blocked waiting on DB relation")
        message = ops_test.model.applications[APP_NAME].units[0].workload_status_message
        assert message == "Waiting for postgres relation to be established."

        logger.info("Making relations")
        await perform_livepatch_integrations(ops_test)
        logger.info("Check for blocked waiting on DB migration")
        await ops_test.model.wait_for_idle(apps=[APP_NAME], status=BLOCKED_STATUS, raise_on_blocked=False, timeout=300)
        logger.info("Running migration action")
        action = await ops_test.model.applications[APP_NAME].units[0].run_action("schema-upgrade")
        action = await action.wait()
        assert action.results["schema-upgrade-required"] == "False"

        logger.info("Waiting for active idle")
        await ops_test.model.wait_for_idle(apps=[APP_NAME], status=ACTIVE_STATUS, raise_on_blocked=False, timeout=300)
        assert ops_test.model.applications[APP_NAME].units[0].workload_status == ACTIVE_STATUS
