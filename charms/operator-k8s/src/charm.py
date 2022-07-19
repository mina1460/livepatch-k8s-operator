#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Livepatch k8s charm.
"""
from urllib.parse import urlparse

from autologging import traced
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import ServiceStatus

import utils
from constants import (LOGGER, SCHEMA_UPGRADE_CONTAINER, WORKLOAD_CONTAINER,
                       PgIsReadyStates)


@traced
class LivepatchCharm(CharmBase):

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.update_status, self.on_update_status)
        self.framework.observe(self.on.leader_elected, self.on_leader_elected)
        self.framework.observe(self.on.start, self.on_start)
        self.framework.observe(self.on.stop, self.on_stop)

        self.framework.observe(self.on.restart_action, self.restart_action)
        self.framework.observe(
            self.on.schema_upgrade_action, self.schema_upgrade_action)
        self.framework.observe(
            self.on.get_resource_token_action, self.get_resource_token_action)

    # Runs first
    def on_config_changed(self, event):
        self._update_workload_container_config(event)

    # Runs second
    def on_start(self, event):
        self._update_workload_container_config(event)

    # Runs third and on any container restarts & does not guarantee the container is "still up"
    # Runs additionally when; a new unit is created, and upgrade-charm has been run
    # def on_pebble_ready(self, event):

    # Runs periodically (5mins by default), supposedly not needed as it was
    # designed to check service health. Now pebble does this.
    def on_update_status(self, _):
        self._ready()

    # Runs AFTER peer-relation-created
    # When a leader loses leadership it only sees the leader-settings-changed
    # As such you will only receive this even if YOU ARE the CURRENT leader (so no need to check)
    def on_leader_elected(self, event):
        self._update_workload_container_config(event)

    def on_stop(self, _):
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if self._ready():
            container.stop()
            self.unit.status = WaitingStatus("stopped")

    def _update_workload_container_config(self, event):
        """
        Update workload with all available configuration
        data.
        """
        container = self.unit.get_container(WORKLOAD_CONTAINER)

        if not self.schema_upgrade_ran():
            LOGGER.error('waiting for schema upgrade')
            self.unit.status = BlockedStatus("waiting for schema upgrade")

        env_vars = utils.map_config_to_env_vars(
            self, LP_SERVER_ADDRESS=":8081")

        env_vars['PATCH_SYNC_TOKEN'] = self.get_resource_token()

        # remove empty environment values
        env_vars = {key: value for key, value in env_vars.items() if value}

        if container.can_connect():
            update_config_environment_layer = {
                "summary": "Livepatch Service",
                "description": "Pebble config layer for livepatch",
                "services": {
                    "livepatch": {
                        "override": "merge",
                        "summary": "Livepatch Service",
                        "command": "/usr/local/bin/livepatch-server",
                        "startup": "disabled",
                        "environment": env_vars,
                    },
                },
                "checks": {
                    "livepatch-check": {
                        "override": "replace",
                        "period": "1m",
                        "http": {
                            "url": "http://localhost:8081/debug/status"
                        }
                    }
                }
            }

            container.add_layer(
                "livepatch", update_config_environment_layer, combine=True)
            if self._ready():
                if container.get_service("livepatch").is_running():
                    container.replan()
                else:
                    container.start('livepatch')
        else:
            LOGGER.info("workload container not ready - defering")
            event.defer()

    def _ready(self):
        if not self.schema_upgrade_ran():
            self.unit.status = WaitingStatus("waiting for schema upgrade")
            return False

        container = self.unit.get_container(WORKLOAD_CONTAINER)

        if container.can_connect():
            plan = container.get_plan()
            if plan.services.get("livepatch") is None:
                LOGGER.error("waiting for service")
                self.unit.status = WaitingStatus("waiting for service")
                return False

            if container.get_service("livepatch").is_running():
                self.unit.status = ActiveStatus("running")
            return True
        else:
            LOGGER.error("cannot connect to workload container")
            self.unit.status = WaitingStatus("waiting for livepatch workload")
            return False

    # Actions
    def restart_action(self, event):
        """
        Restarts the workload container
        """
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if self.can_connect() and self._ready():
            container.restart()

    def schema_upgrade_action(self, event):
        """
        Performs a schema upgrade on the configurable database
        """
        if not self.unit.is_leader():
            return

        container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not container.can_connect():
            LOGGER.error("cannot connect to the schema update container")
            return

        if not container.exists('/usr/bin/pg_isready'):
            LOGGER.error(
                'pg_isready not found in the schema upgrade container')
            self.unit.status = BlockedStatus("cannot run schema upgrade")
            return

        conn_string = self.config.get("database-connection-string")
        if not conn_string:
            LOGGER.error(
                'database-connection-string not specified: cannot run schema upgrade')
            self.unit.status = BlockedStatus(
                "database-connection-string not specified")
            return
        parsed_conn_string = urlparse(conn_string)

        db_ready_process = container.exec(command=['/usr/bin/pg_isready'], environment={
            'PGUSER': parsed_conn_string.username,
            'PGPASSWORD': parsed_conn_string.password,
            'PGHOST': parsed_conn_string.hostname,
            'PGPORT': parsed_conn_string.port,
            'PGDATABASE': parsed_conn_string.path[1:]
        })

        stdout, stderr = db_ready_process.wait_output()
        if stdout == PgIsReadyStates.CONNECTED:
            self.unit.status = WaitingStatus(
                "pg connection successful, attempting upgrade")
            if not container.exists('/usr/local/bin/livepatch-schema-tool'):
                LOGGER.error(
                    'livepatch-schema-tool not found in the schema upgrade container')
                self.unit.status = BlockedStatus(
                    "cannot run schema upgrade tool")
                return

            # postgresql is ready: execute the livepatch-schema-tool upgrade
            process = container.exec(command=[
                "/usr/local/bin/livepatch-schema-tool upgrade /usr/src/livepatch/schema-upgrades"
            ], environment={
                "DB": conn_string
            })
            _, stderr = process.wait_output()
            if stderr != "":
                self.unit.status = BlockedStatus(
                    "Failed to run schema migration")
                event.set_results({'error': 'err'})
                return
            else:
                self.unit.status = WaitingStatus("Schema migration done")
                event.set_results({'result': 'done'})
                self.set_schema_upgrade_ran()

        elif stdout == PgIsReadyStates.REJECTED:
            self.unit.status = WaitingStatus(
                'server rejected connection, may be starting up')
            LOGGER.error('server rejected connection, may be starting up')
            event.set_results(
                {'error': 'server rejected connection, may be starting up'})
            return
        elif stdout == PgIsReadyStates.NO_RESPONSE:
            self.unit.status = BlockedStatus(
                'no response at specified address, please check your db configuration')
            LOGGER.error(stderr)
            event.set_results(
                {'error': 'no response at specified address, please check your db configuration'})
            return
        elif stdout == PgIsReadyStates.NO_ATTEMPT:
            self.unit.status = BlockedStatus(
                'invalid parameters - something went wrong in the charm code')
            LOGGER.error(stderr)
            event.set_results(
                {'error': 'invalid parameters - something went wrong in the charm code'})
            return
        else:
            self.unit.status = BlockedStatus(
                "something went wrong in the charm code")
            LOGGER.error(stderr)
            event.set_results({'error': stderr})
            return

        self._update_workload_container_config(event)

    def set_schema_upgrade_ran(self):
        # get the peer relation.
        peer_relation = self.model.get_relation("livepatch")
        # if it does not exist, return.
        if not peer_relation:
            return

        peer_relation.data[self.app].update({'schema-upgraded': 'done'})

    def schema_upgrade_ran(self) -> bool:
        # get the peer relation.
        peer_relation = self.model.get_relation("livepatch")
        # if it does not exist, return.
        if not peer_relation:
            return False
        # if relation already contains 'schema-created' that
        # means the schema has already been created.
        return bool(peer_relation.data.get(self.app, {}).get('schema-upgraded', False))
        
    def set_resource_token(self, resource_token: str):
        # get the peer relation.
        peer_relation = self.model.get_relation("livepatch")
        # if it does not exist, return.
        if not peer_relation:
            return False
        peer_relation.data[self.app].update({'resource-token': resource_token})

    def get_resource_token(self) -> str:
        # get the peer relation.
        peer_relation = self.model.get_relation("livepatch")
        # if it does not exist, return.
        if not peer_relation:
            return ''
        return peer_relation.data.get(self.app, {}).get('resource-token', None)

    def get_resource_token_action(self, event):
        """
        Retrieves the livepatch resource token from ua-contracts.
        """
        if not self.unit.is_leader():
            LOGGER.error(
                'cannot fetch the resource token: unit is not the leader')
            event.set_results(
                {'error': 'cannot fetch the resource token: unit is not the leader'})
            return

        peer_relation = self.model.get_relation('livepatch')
        if not peer_relation:
            LOGGER.error(
                'cannot fetch the resource token: peer relation not ready')
            event.set_results(
                {'error': 'cannot fetch the resource token: peer relation not ready'})
            return

        contract_token = event.params.get('contract-token', '')
        proxies = utils.get_proxy_dict(self.config)
        contracts_url = self.config.get('contracts-url', '')
        machine_token = utils.get_machine_token(
            contract_token, contracts_url=contracts_url, proxies=proxies)

        if not machine_token:
            LOGGER.error('failed to retrieve the machine token')
            event.set_results(
                {'error': 'cannot fetch the resource token: failed to fetch the machine token'})
            return

        resource_token = utils.get_resource_token(
            machine_token, contracts_url=contracts_url, proxies=proxies)

        self.set_resource_token(resource_token)
        event.set_results({'result': 'resource token set'})


if __name__ == "__main__":
    main(LivepatchCharm, use_juju_for_storage=True)
