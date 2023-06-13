import logging

import pytest_asyncio
from ops.model import ActiveStatus
from pytest_operator.plugin import OpsTest

from utils import fetch_charm

logger = logging.getLogger(__name__)
APP_NAME = "livepatch"
DB_NAME = "postgresql"


@pytest_asyncio.fixture(scope="module")
async def app(ops_test: OpsTest):
    logger.info("Building local charm")
    charm = await fetch_charm(ops_test)
    resources = {
        "livepatch-server-image": "localhost:32000/livepatch-server:latest",
        "livepatch-schema-upgrade-tool-image": "localhost:32000/livepatch-schema-tool:latest",
    }
    config = {"server.url-template": "https://localhost:8080/{filename}"}
    application = await ops_test.model.deploy(
        charm, series="focal", resources=resources, application_name=APP_NAME, config=config
    )
    await ops_test.model.deploy(
        "postgresql-k8s",
        series="focal",
        channel="edge",
        trust=True,
        application_name="postgresql",
    )
    await ops_test.model.relate(APP_NAME, DB_NAME + ":database")
    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(
            status=ActiveStatus.name,
            timeout=1000,
        )
        app = ops_test.model.applications[APP_NAME]
        assert app.units[0].workload_status == "active"
    yield application
