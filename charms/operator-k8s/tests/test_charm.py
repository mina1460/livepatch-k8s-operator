# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

import ops
import responses
from ops.testing import Harness
from src.charm import LivepatchCharm
from src.constants import WORKLOAD_CONTAINER


class MockOutput:
    def __init__(self, stdout, stderr):
        self._stdout = stdout
        self._stderr = stderr

    def wait_output(self):

        return self._stdout, self._stderr


def MockExec(_, command, environment):
    if len(command) != 1:
        return MockOutput('', 'unexpected number of commands')
    if command[0] == '/usr/bin/pg_isready':
        return MockOutput(0, '')
    elif command[0] == '/usr/local/bin/livepatch-schema-tool upgrade /usr/src/livepatch/schema-upgrades':
        return MockOutput('', '')
    return MockOutput('', 'unexpected command')


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(LivepatchCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.disable_hooks()
        self.harness.add_oci_resource('livepatch-server-image')
        self.harness.begin()

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.harness.charm.framework.charm_dir = pathlib.Path(
            self.tempdir.name)

        self.harness.container_pebble_ready('livepatch')

    def test_on_config_changed(self):
        rel_id = self.harness.add_relation('livepatch', 'livepatch')
        self.harness.add_relation_unit(rel_id, 'livepatch/1')
        self.harness.set_leader(True)

        self.harness.update_relation_data(
            rel_id,
            'livepatch',
            {
                'schema-upgraded': 'done',
                'resource-token': 'test-token',
            }
        )

        container = self.harness.model.unit.get_container("livepatch")
        self.harness.charm.on.livepatch_pebble_ready.emit(container)

        self.harness.update_config({
            'auth-basic-users': 'user1:phash1,user2:phash2',
            'auth-sso-enabled': True,
            'contracts-enabled': True,
            'contracts-url': 'https://contracts.canonical.com',
            'contracts-user': 'test-user',
            'contracts-password': 'test-password',
            'database-connection-string': 'postgresql://test',
            'patch-storage-type': 'filesystem',
            'patch-storage-filesystem-path': '/srv/',
            'patch-cache-enabled': True,
        })
        self.harness.charm.on.config_changed.emit()

        # Emit the pebble-ready event for livepatch
        self.harness.charm.on.livepatch_pebble_ready.emit(container)

        # Check the that the plan was updated
        plan = self.harness.get_container_pebble_plan("livepatch")
        self.assertEqual(
            plan.to_dict(),
            {
                'services': {
                    'livepatch': {
                        'override': 'merge',
                        'startup': 'disabled',
                        'summary': 'Livepatch Service',
                        'command': '/usr/local/bin/livepatch-server',
                        'environment': {
                            'AUTH_BASIC_USERS': 'user1:phash1,user2:phash2',
                            'AUTH_SSO_ENABLED': True,
                            'AUTH_SSO_URL': 'login.ubuntu.com',
                            'CONTRACTS_ENABLED': True,
                            'CONTRACTS_PASSWORD': 'test-password',
                            'CONTRACTS_URL': 'https://contracts.canonical.com',
                            'CONTRACTS_USER': 'test-user',
                            'DATABASE_CONNECTION_STRING': 'postgresql://test',
                            'DATABASE_LIFETIME_MAX': '30m',
                            'DATABASE_POOL_MAX': 20,
                            'KPI_REPORTS_INTERVAL': '1hr',
                            'LP_SERVER_ADDRESS': ':8081',
                            'LP_SERVER_IS_LEADER': True,
                            'MACHINE_REPORTS_DB_CLEANUP_INTERVAL': '6hr',
                            'MACHINE_REPORTS_DB_CLEANUP_ROW_LIMIT': 1000,
                            'MACHINE_REPORTS_DB_RETENTION_DAYS': 30,
                            'PATCH_BLOCKLIST_REFRESH_INTERVAL': '1hr',
                            'PATCH_CACHE_ENABLED': True,
                            'PATCH_CACHE_SIZE': 128,
                            'PATCH_CACHE_TTL': '1h',
                            'PATCH_STORAGE_FILESYSTEM_PATH': '/srv/',
                            'PATCH_STORAGE_TYPE': 'filesystem',
                            'PATCH_SYNC_DOWNLOAD_INTERVAL': '1h',
                            'PATCH_SYNC_FLAVOURS': 'generic,lowlatency,aws',
                            'PATCH_SYNC_TOKEN': 'test-token',
                            'PATCH_SYNC_UPSTREAM_URL': 'https://livepatch.canonical.com',
                            'SERVER_BURST_LIMIT': 500,
                            'SERVER_CONCURRENCY_LIMIT': 50,
                            'SERVER_LOG_LEVEL': 'info'
                        }
                    }
                }
            },
        )

    def test_peer_relation_schema_upgrade(self):
        rel_id = self.harness.add_relation('livepatch', 'livepatch')
        self.harness.add_relation_unit(rel_id, 'livepatch/1')

        self.harness.set_leader(True)

        value = self.harness.charm.schema_upgrade_ran()
        self.assertEqual(value, False)

        self.harness.charm.set_schema_upgrade_ran()

        value = self.harness.charm.schema_upgrade_ran()
        self.assertEqual(value, True)

    def test_peer_relation_resource_token(self):
        rel_id = self.harness.add_relation('livepatch', 'livepatch')
        self.harness.add_relation_unit(rel_id, 'livepatch/1')

        self.harness.set_leader(True)

        value = self.harness.charm.get_resource_token()
        self.assertEqual(value, None)

        self.harness.charm.set_resource_token('test-resource-token')

        value = self.harness.charm.get_resource_token()
        self.assertEqual(value, 'test-resource-token')

    @responses.activate
    def test_get_resource_token_action(self):
        responses.add(
            responses.POST,
            'https://contracts.canonical.com/v1/context/machines/token',
            json={'machineToken': 'test-machine-token'},
            status=200,
        )
        responses.add(
            responses.GET,
            'https://contracts.canonical.com/v1/resources/livepatch-onprem/context/machines/livepatch-onprem',
            json={'resourceToken': 'test-resource-token'},
            status=200,
        )

        self.harness.update_config({
            'contracts-url': 'https://contracts.canonical.com',
        })
        self.harness.charm.on.config_changed.emit()

        rel_id = self.harness.add_relation('livepatch', 'livepatch')
        self.harness.add_relation_unit(rel_id, 'livepatch/1')
        self.harness.set_leader(True)

        action_event = Mock(
            params={'contract-token': 'test-contract-token', 'fail': ''})
        self.harness.charm.get_resource_token_action(action_event)

        data = self.harness.get_relation_data(rel_id, 'livepatch')
        self.assertEqual(data, {'resource-token': 'test-resource-token'})

    @patch.object(ops.model.Container, 'exec', MockExec)
    def test_schema_upgrade_action(self):
        self.harness.update_config({
            'database-connection-string': 'postgresql://user:password@hostname/livepatch'
        })
        # Emit the pebble-ready event for livepatch
        container = self.harness.model.unit.get_container(
            "livepatch-schema-upgrade")
        self.harness.charm.on.livepatch_schema_upgrade_pebble_ready.emit(
            container)
        self.harness.set_can_connect(container, True)
        self.harness.charm.on.config_changed.emit()

        self._push_happy_script(container, '/usr/bin/pg_isready')
        self._push_happy_script(
            container, '/usr/local/bin/livepatch-schema-tool')

        rel_id = self.harness.add_relation('livepatch', 'livepatch')
        self.harness.add_relation_unit(rel_id, 'livepatch/1')
        self.harness.set_leader(True)

        action_event = Mock(params={'fail': ''})
        self.harness.charm.schema_upgrade_action(action_event)

        data = self.harness.get_relation_data(rel_id, 'livepatch')
        self.assertEqual(data, {'schema-upgraded': 'done'})

    def _push_happy_script(self, container, filename: str):
        script = '''#!/bin/bash
        exit 0
        '''
        data = bytes(script, 'utf-8')
        container.push(filename, 'data', make_dirs=True)
