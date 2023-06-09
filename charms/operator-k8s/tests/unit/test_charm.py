# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from ops.testing import Harness

from src.charm import LivepatchCharm

APP_NAME = "canonical-livepatch-server-k8s"


class MockOutput:
    def __init__(self, stdout, stderr):
        self._stdout = stdout
        self._stderr = stderr

    def wait_output(self):
        return self._stdout, self._stderr


def mock_exec(_, command, environment):
    if len(command) != 1:
        return MockOutput("", "unexpected number of commands")
    if command[0] == "/usr/bin/pg_isready":
        return MockOutput(0, "")
    elif command[0] == "/usr/local/bin/livepatch-schema-tool upgrade /usr/src/livepatch/schema-upgrades":
        return MockOutput("", "")
    return MockOutput("", "unexpected command")


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(LivepatchCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.disable_hooks()
        self.harness.add_oci_resource("livepatch-server-image")
        self.harness.add_oci_resource("livepatch-schema-upgrade-tool-image")
        self.harness.begin()

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.harness.charm.framework.charm_dir = pathlib.Path(self.tempdir.name)

        self.harness.container_pebble_ready("livepatch")
        self.harness.container_pebble_ready("livepatch-schema-upgrade")

    def test_on_config_changed(self):
        rel_id = self.harness.add_relation("livepatch", "livepatch")
        self.harness.add_relation_unit(rel_id, f"{APP_NAME}/1")
        self.harness.set_leader(True)

        self.harness.update_relation_data(
            rel_id,
            APP_NAME,
            {"schema-upgraded": "done", "resource-token": "test-token", "db-uri": "postgres://123"},
        )

        container = self.harness.model.unit.get_container("livepatch")
        with patch("src.charm.LivepatchCharm.migration_is_required") as migration:
            migration.return_value = False
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

            self.harness.update_config(
                {
                    "auth.sso.enabled": True,
                    "patch-storage.type": "filesystem",
                    "patch-storage.filesystem-path": "/srv/",
                    "patch-cache.enabled": True,
                }
            )
            self.harness.charm.on.config_changed.emit()

            # Emit the pebble-ready event for livepatch
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

        # Check the that the plan was updated
        plan = self.harness.get_container_pebble_plan("livepatch")
        required_environment = {
            "LP_AUTH_SSO_ENABLED": True,
            "LP_PATCH_STORAGE_TYPE": "filesystem",
            "LP_PATCH_STORAGE_FILESYSTEM_PATH": "/srv/",
            "LP_PATCH_CACHE_ENABLED": True,
            "LP_DATABASE_CONNECTION_STRING": "postgres://123",
        }
        environment = plan.to_dict()["services"]["livepatch"]["environment"]
        self.assertEqual(environment, environment | required_environment)

    def test_peer_relation_resource_token(self):
        rel_id = self.harness.add_relation("livepatch", "livepatch")
        self.harness.add_relation_unit(rel_id, f"{APP_NAME}/1")

        self.harness.set_leader(True)

        value = self.harness.charm.get_resource_token()
        self.assertEqual(value, None)

        self.harness.charm.set_resource_token("test-resource-token")

        value = self.harness.charm.get_resource_token()
        self.assertEqual(value, "test-resource-token")
