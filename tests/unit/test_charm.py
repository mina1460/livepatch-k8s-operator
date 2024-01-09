# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import patch

from ops.model import BlockedStatus
from ops.testing import Harness

from src.charm import LivepatchCharm

APP_NAME = "canonical-livepatch-server-k8s"

TEST_TOKEN = "test-token"  # nosec


class MockOutput:
    """A wrapper class for command output and errors."""

    def __init__(self, stdout, stderr):
        self._stdout = stdout
        self._stderr = stderr

    def wait_output(self):
        """Return the stdout and stderr from running the command."""
        return self._stdout, self._stderr


def mock_exec(_, command, environment) -> MockOutput:
    """Mock Execute the commands."""
    if len(command) != 1:
        return MockOutput("", "unexpected number of commands")
    cmd: str = command[0]
    if cmd == "/usr/bin/pg_isready":
        return MockOutput(0, "")
    if cmd == "/usr/local/bin/livepatch-schema-tool upgrade /usr/src/livepatch/schema-upgrades":
        return MockOutput("", "")
    return MockOutput("", "unexpected command")


class TestCharm(unittest.TestCase):
    """A wrapper class for charm unit tests."""

    def setUp(self):
        self.harness = Harness(LivepatchCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.disable_hooks()
        self.harness.add_oci_resource("livepatch-server-image")
        self.harness.add_oci_resource("livepatch-schema-upgrade-tool-image")
        self.harness.begin()
        rel_id = self.harness.add_relation("livepatch", "livepatch")
        self.harness.add_relation_unit(rel_id, f"{APP_NAME}/1")
        self.harness.container_pebble_ready("livepatch")
        self.harness.container_pebble_ready("livepatch-schema-upgrade")

    def test_on_config_changed(self):
        """A test for config changed hook."""
        self.harness.set_leader(True)

        self.harness.charm._state.dsn = "postgres://123"
        self.harness.charm._state.resource_token = TEST_TOKEN

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
                    "server.url-template": "http://localhost/{filename}",
                    "server.is-hosted": True,
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

    def test_missing_url_template_config_causes_blocked_state(self):
        """A test for missing url template."""
        self.harness.set_leader(True)

        self.harness.charm._state.dsn = "postgres://123"
        self.harness.charm._state.resource_token = TEST_TOKEN

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
                    "server.is-hosted": True,
                }
            )
            self.harness.charm.on.config_changed.emit()

            # Emit the pebble-ready event for livepatch
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

        # Check the that the plan was updated
        plan = self.harness.get_container_pebble_plan("livepatch")
        self.assertEqual(plan.to_dict(), {})
        self.assertEqual(self.harness.charm.unit.status.name, BlockedStatus.name)
        self.assertEqual(self.harness.charm.unit.status.message, "✘ server.url-template config not set")

    def test_missing_sync_token_causes_blocked_state(self):
        """For on-prem servers, a missing sync token should cause a blocked state."""
        self.harness.set_leader(True)

        self.harness.charm._state.dsn = "postgres://123"
        # self.harness.charm._state.resource_token = ""

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
                    "server.url-template": "http://localhost/{filename}",
                    "server.is-hosted": False,
                }
            )
            self.harness.charm.on.config_changed.emit()

            # Emit the pebble-ready event for livepatch
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

        # Check the that the plan was updated
        plan = self.harness.get_container_pebble_plan("livepatch")
        self.assertEqual(plan.to_dict(), {})
        self.assertEqual(self.harness.charm.unit.status.name, BlockedStatus.name)
        self.assertEqual(
            self.harness.charm.unit.status.message, "✘ patch-sync token not set, run get-resource-token action"
        )

    def test_logrotate_config_pushed(self):
        """Assure that logrotate config is pushed."""
        # Trigger config-changed so that logrotate config gets written
        self.harness.charm.on.config_changed.emit()

        # Ensure that the content looks sensible
        root = self.harness.get_filesystem_root("livepatch")
        config = (root / "etc/logrotate.d/livepatch").read_text()
        self.assertIn("/var/log/livepatch {", config)

    def test_database_relations_are_mutually_exclusive__legacy_first(self):
        """Assure that database relations are mutually exclusive."""
        self.harness.set_leader(True)
        self.harness.enable_hooks()

        legacy_db_rel_id = self.harness.add_relation("database-legacy", "postgres")

        # The `ops-lib-pgsql` library calls `leader-get` and `leader-set` tools
        # from juju help-tools, so we need to mock calls that try to spawn a
        # subprocess.
        with patch("subprocess.check_call", return_value=None):  # Stubs `leader-set` call.
            with patch("subprocess.check_output", return_value=b""):  # Stubs `leader-get` call.
                self.harness.add_relation_unit(legacy_db_rel_id, "postgres/0")
        self.harness.update_relation_data(legacy_db_rel_id, "postgres", {})

        db_rel_id = self.harness.add_relation("database", "postgres-new")
        self.harness.add_relation_unit(db_rel_id, "postgres-new/0")
        with self.assertRaises(Exception) as cm:
            self.harness.update_relation_data(
                db_rel_id,
                "postgres-new",
                {
                    "username": "some-username",
                    "password": "some-password",
                    "endpoints": "some.database.host,some.other.database.host",
                },
            )
        self.assertEqual(
            str(cm.exception),
            "Integration with both database relations is not allowed; `database-legacy` is already activated.",
        )

    def test_database_relations_are_mutually_exclusive__standard_first(self):
        """Assure that database relations are mutually exclusive."""
        self.harness.set_leader(True)
        self.harness.enable_hooks()

        db_rel_id = self.harness.add_relation("database", "postgres-new")
        self.harness.add_relation_unit(db_rel_id, "postgres-new/0")
        self.harness.update_relation_data(
            db_rel_id,
            "postgres-new",
            {
                "username": "some-username",
                "password": "some-password",
                "endpoints": "some.database.host,some.other.database.host",
            },
        )

        legacy_db_rel_id = self.harness.add_relation("database-legacy", "postgres")

        with self.assertRaises(Exception) as cm:
            # The `ops-lib-pgsql` library calls `leader-get` and `leader-set` tools
            # from juju help-tools, so we need to mock calls that try to spawn a
            # subprocess.
            with patch("subprocess.check_call", return_value=None):  # Stubs `leader-set` call.
                with patch("subprocess.check_output", return_value=b""):  # Stubs `leader-get` call.
                    self.harness.add_relation_unit(legacy_db_rel_id, "postgres/0")

        self.assertEqual(
            str(cm.exception),
            "Integration with both database relations is not allowed; `database` is already activated.",
        )

    def test_postgres_patch_storage_config_sets_in_container(self):
        """A test for postgres patch storage config in container."""
        self.harness.set_leader(True)

        self.harness.charm._state.dsn = "postgres://123"
        self.harness.charm._state.resource_token = TEST_TOKEN

        container = self.harness.model.unit.get_container("livepatch")
        with patch("src.charm.LivepatchCharm.migration_is_required") as migration:
            migration.return_value = False
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

            self.harness.update_config(
                {
                    "patch-storage.type": "postgres",
                    "patch-storage.postgres-connection-string": "postgres://user:password@host/db",
                    "server.url-template": "http://localhost/{filename}",
                    "server.is-hosted": True,
                }
            )
            self.harness.charm.on.config_changed.emit()

            # Emit the pebble-ready event for livepatch
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

        # Check the that the plan was updated
        plan = self.harness.get_container_pebble_plan("livepatch")
        required_environment = {
            "LP_PATCH_STORAGE_TYPE": "postgres",
            "LP_PATCH_STORAGE_POSTGRES_CONNECTION_STRING": "postgres://user:password@host/db",
        }
        environment = plan.to_dict()["services"]["livepatch"]["environment"]
        self.assertEqual(environment, environment | required_environment)

    def test_postgres_patch_storage_config_defaults_to_database_relation(self):
        """A test for postgres patch storage config."""
        self.harness.set_leader(True)
        self.harness.enable_hooks()

        db_rel_id = self.harness.add_relation("database", "postgres-new")
        self.harness.add_relation_unit(db_rel_id, "postgres-new/0")
        self.harness.update_relation_data(
            db_rel_id,
            "postgres-new",
            {
                "username": "username",
                "password": "password",
                "endpoints": "host",
            },
        )

        container = self.harness.model.unit.get_container("livepatch")
        with patch("src.charm.LivepatchCharm.migration_is_required") as migration:
            migration.return_value = False
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

            self.harness.update_config(
                {
                    "patch-storage.type": "postgres",
                    "server.url-template": "http://localhost/{filename}",
                    "server.is-hosted": True,
                }
            )
            self.harness.charm.on.config_changed.emit()

            # Emit the pebble-ready event for livepatch
            self.harness.charm.on.livepatch_pebble_ready.emit(container)

        # Check the that the plan was updated
        plan = self.harness.get_container_pebble_plan("livepatch")
        required_environment = {
            "LP_PATCH_STORAGE_TYPE": "postgres",
            "LP_PATCH_STORAGE_POSTGRES_CONNECTION_STRING": "postgresql://username:password@host/livepatch-server",
        }
        environment = plan.to_dict()["services"]["livepatch"]["environment"]
        self.assertEqual(environment, environment | required_environment)
