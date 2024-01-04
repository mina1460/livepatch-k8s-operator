#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import pytest
import requests
from conftest import APP_NAME
from ops.model import ActiveStatus
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
@pytest.mark.usefixtures("app")
class TestDeployment:
    async def test_application_is_up(self, ops_test: OpsTest):
        """The app is up and running."""

        logger.info("Getting model status")
        status = await ops_test.model.get_status()  # noqa: F821
        logger.info(f"Status: {status}")
        assert ops_test.model.applications[APP_NAME].status == ActiveStatus.name

        address = status["applications"][APP_NAME]["units"][f"{APP_NAME}/0"]["address"]

        url = f"http://{address}:8080/debug/status"
        logger.info("Querying app address: %s", url)
        r = requests.get(url, timeout=2.0)
        assert r.status_code == 200
        logger.info(f"Output = {r.json()}")
