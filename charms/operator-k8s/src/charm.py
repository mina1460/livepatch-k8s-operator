#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Livepatch k8s charm.
"""
import pgsql
from charms.nginx_ingress_integrator.v0.ingress import IngressRequires
from ops import pebble
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus

import utils
from constants import LOGGER, SCHEMA_UPGRADE_CONTAINER, WORKLOAD_CONTAINER

SERVER_PORT = 8080
DATABASE_NAME = "livepatch-server"


class LivepatchCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.update_status, self.on_update_status)
        self.framework.observe(self.on.leader_elected, self.on_leader_elected)
        self.framework.observe(self.on.start, self.on_start)
        self.framework.observe(self.on.stop, self.on_stop)

        self.framework.observe(self.on.restart_action, self.restart_action)
        self.framework.observe(self.on.schema_upgrade_action, self.schema_upgrade_action)
        self.framework.observe(self.on.schema_version_action, self.schema_version_check_action)

        self.framework.observe(self.on.get_resource_token_action, self.get_resource_token_action)

        # Database
        self.db = pgsql.PostgreSQLClient(self, "db")
        self.framework.observe(
            self.db.on.database_relation_joined,
            self._on_database_relation_joined,
        )
        self.framework.observe(self.db.on.master_changed, self._on_master_changed)
        self.framework.observe(self.db.on.standby_changed, self._on_standby_changed)

        self.ingress = IngressRequires(
            self,
            {
                "service-hostname": self.config["external_hostname"],
                "service-name": self.app.name,
                "service-port": 8080,
            },
        )

    # Runs first
    def on_config_changed(self, event):
        self._update_workload_container_config(event)

    # Runs second
    def on_start(self, event):
        self._update_workload_container_config(event)

    # Runs third and on any container restarts & does not guarantee the container is "still up"
    # Runs additionally when; a new unit is created, and upgrade-charm has been run
    # def on_pebble_ready(self, event):
    #    self._update_workload_container_config(event)

    def on_update_status(self, event):
        workload = self.unit.get_container(WORKLOAD_CONTAINER)
        self._ready(workload)

    # Runs AFTER peer-relation-created
    # When a leader loses leadership it only sees the leader-settings-changed
    # As such you will only receive this even if YOU ARE the CURRENT leader (so no need to check)
    def on_leader_elected(self, event):
        self._update_workload_container_config(event)

    def on_stop(self, _):
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if container.can_connect():
            container.stop("livepatch")
            self.unit.status = WaitingStatus("stopped")

    def _update_workload_container_config(self, event):
        """
        Update workload with all available configuration
        data.
        """
        db_uri = self._get_db_uri()
        if not db_uri:
            LOGGER.info("waiting for PG connection string")
            self.unit.status = BlockedStatus("waiting for pg relation.")
            event.defer()
            return
        schema_container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not schema_container.can_connect():
            LOGGER.error("cannot connect to the schema update container")
            self.unit.status = WaitingStatus("Waiting to connect - schema container.")
            event.defer()
            return

        upgrade_required = False
        try:
            upgrade_required = self.migration_is_required(schema_container, db_uri)
        except Exception as e:
            LOGGER.error("Failed to determe if schema upgrade required.")
            LOGGER.error(e)
            return
        if upgrade_required:
            if self.unit.is_leader():
                self.schema_upgrade(schema_container, db_uri)
            else:
                LOGGER.error("waiting for schema upgrade")
                self.unit.status = WaitingStatus("waiting for schema upgrade")
                event.defer()
                return

        workload_container = self.unit.get_container(WORKLOAD_CONTAINER)

        env_vars = utils.map_config_to_env_vars(self)

        # Some extra config
        env_vars["PATCH_SYNC_TOKEN"] = self.get_resource_token()
        env_vars["LP_DATABASE_CONNECTION_STRING"] = db_uri
        env_vars["LP_SERVER_SERVER_ADDRESS"] = f":{SERVER_PORT}"
        if self.config.get("patch-sync.enabled") is True:
            # TODO: Find a better way to identify a on-prem syncing instance.
            env_vars["LP_PATCH_SYNC_ID"] = self.model.uuid

        # remove empty environment values
        env_vars = {key: value for key, value in env_vars.items() if value}
        if workload_container.can_connect():
            update_config_environment_layer = {
                "services": {
                    "livepatch": {
                        "summary": "Livepatch Service",
                        "description": "Pebble config layer for livepatch",
                        "override": "merge",
                        "startup": "disabled",
                        "command": "/usr/local/bin/livepatch-server",
                        "environment": env_vars,
                    },
                },
                "checks": {
                    "livepatch-check": {
                        "override": "replace",
                        "period": "1m",
                        "http": {"url": f"http://localhost:{SERVER_PORT}/debug/status"},
                    }
                },
            }

            workload_container.add_layer("livepatch", update_config_environment_layer, combine=True)
            if self._ready(workload_container):
                if workload_container.get_service("livepatch").is_running():
                    workload_container.replan()
                else:
                    workload_container.start("livepatch")
            else:
                self.unit.status = WaitingStatus("Service is not ready")
                return
        else:
            LOGGER.info("workload container not ready - deferring")
            self.unit.status = WaitingStatus("Waiting to connect - workload container")
            event.defer()
            return

        self.unit.status = ActiveStatus()

    def _ready(self, workload_container):
        if workload_container.can_connect():
            plan = workload_container.get_plan()
            if plan.services.get("livepatch") is None:
                LOGGER.info("livepatch service is not ready yet")
                return False
            if workload_container.get_service("livepatch").is_running():
                self.unit.status = ActiveStatus()
            return True
        else:
            LOGGER.error("cannot connect to workload container")
            return False

    # Database

    def _on_database_relation_joined(self, event: pgsql.DatabaseRelationJoinedEvent) -> None:
        """
        Handles determining if the database has finished setup, once setup is complete
        a master/standby may join / change in consequent events.
        """
        LOGGER.info("(postgresql) RELATION_JOINED event fired.")

        if self.model.unit.is_leader():
            # Handle database configurations / changes here!
            event.database = DATABASE_NAME
        elif event.database != DATABASE_NAME:
            event.defer()

    def _on_master_changed(self, event: pgsql.MasterChangedEvent) -> None:
        """
        Handles master units of postgres joining / changing.
        The internal snap configuration is updated to reflect this.
        """
        LOGGER.info("(postgresql) MASTER_CHANGED event fired.")

        if event.database != DATABASE_NAME:
            LOGGER.debug("Database setup not complete yet, returning.")
            return

        if self.model.unit.is_leader():
            self.set_status_and_log("Updating application database connection...", WaitingStatus)
            peer_relation = self.model.get_relation("livepatch")
            if not peer_relation:
                raise ValueError("Peer relation not found")
            conn_str = None if event.master is None else event.master.conn_str
            db_uri = None if event.master is None else event.master.uri
            if conn_str:
                peer_relation.data[self.app].update({"conn-str": conn_str})
            if db_uri:
                peer_relation.data[self.app].update({"db-uri": db_uri})

        self.on_config_changed(event)

    def _on_standby_changed(self, event: pgsql.StandbyChangedEvent):
        LOGGER.info("(postgresql) STANDBY_CHANGED event fired.")
        # NOTE NOTE NOTE
        # This should be used for none-master on-prem instances when configuring
        # additional livepatch instances, enabling us to read from standbys
        if event.database != DATABASE_NAME:
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return

        # If read only replicas are desired, these urls should be added to
        # the peer relation e.g. peer = `[c.uri for c in event.standbys]`

    # Actions
    def restart_action(self, event):
        """
        Restarts the workload container
        """
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if container.can_connect() and container.get_service("livepatch").is_running():
            container.restart()

    def schema_upgrade_action(self, event):
        db_uri = self._get_db_uri()
        container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not db_uri:
            LOGGER.error("DB connection string not set")
            return
        if not container.can_connect():
            LOGGER.error("Cannot connect to the schema update container")
            return
        self.schema_upgrade(container, db_uri)

    def schema_upgrade(self, container, conn_str):
        """
        Performs a schema upgrade on the configurable database
        """
        if not self.unit.is_leader():
            LOGGER.warning("Attempted to run schema upgrade on non-leader unit. Skipping.")
            return

        self.unit.status = WaitingStatus("pg connection successful, attempting upgrade")
        if not container.exists("/usr/local/bin/livepatch-schema-tool"):
            LOGGER.error("livepatch-schema-tool not found in the schema upgrade container")
            self.unit.status = BlockedStatus("Cannot find schema upgrade tool")
            return

        process = None
        try:
            process = container.exec(
                command=[
                    "/usr/local/bin/livepatch-schema-tool",
                    "upgrade",
                    "/etc/livepatch/schema-upgrades",
                    "--db",
                    conn_str,
                ],
            )
        except pebble.APIError as e:
            LOGGER.error(e)
            self.unit.status = BlockedStatus("Schema migration failed")
            return

        try:
            stdout, _ = process.wait_output()
            LOGGER.info(stdout)
            self.unit.status = WaitingStatus("Schema migration done")
        except pebble.ExecError as e:
            LOGGER.error(e)
            LOGGER.error("Exited with code %d. Stderr:", e.exit_code)
            for line in e.stderr.splitlines():
                LOGGER.error("    %s", line)
            self.unit.status = BlockedStatus("Schema migration failed - executing migration failed")
            return

    def schema_version_check_action(self, event) -> str:
        db_uri = self._get_db_uri()
        container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not container.can_connect():
            LOGGER.error("cannot connect to the schema update container")
            return
        self.migration_is_required(container, db_uri)

    def migration_is_required(self, container, conn_str: str) -> bool:
        if not self.unit.is_leader():
            LOGGER.warning("Schema check skipped, can only run on leader.")
            return None

        """Runs a schema version check against the database"""
        if not container.exists("/usr/local/bin/livepatch-schema-tool"):
            LOGGER.error("livepatch-schema-tool not found in the schema upgrade container")
            raise ValueError("Failed to find schema tool")

        if not conn_str:
            LOGGER.error("Database connection string not found")
            raise ValueError("Database connection string is None")

        process = None
        try:
            process = container.exec(
                command=[
                    "/usr/local/bin/livepatch-schema-tool",
                    "check",
                    "/etc/livepatch/schema-upgrades",
                    "--db",
                    conn_str,
                ],
            )
        except pebble.APIError as e:
            LOGGER.error(e)
            raise e

        stdout = None
        try:
            stdout, _ = process.wait_output()
            LOGGER.info("Schema is up to date.")
            LOGGER.info(stdout)
            return False
        except pebble.ExecError as e:
            LOGGER.info(e.stderr)
            if e.exit_code == 2:
                # If command has a non-zero exit code then migrations are pending.
                LOGGER.info("Migrations pending")
                return True
            else:
                # Other exit codes indicate a problem
                raise e

    def set_resource_token(self, resource_token: str):
        # get the peer relation.
        peer_relation = self.model.get_relation("livepatch")
        # if it does not exist, return.
        if not peer_relation:
            return False
        peer_relation.data[self.app].update({"resource-token": resource_token})

    def get_resource_token(self) -> str:
        # get the peer relation.
        peer_relation = self.model.get_relation("livepatch")
        # if it does not exist, return.
        if not peer_relation:
            return ""
        return peer_relation.data.get(self.app, {}).get("resource-token", None)

    def get_resource_token_action(self, event):
        """
        Retrieves the livepatch resource token from ua-contracts.
        """
        if not self.unit.is_leader():
            LOGGER.error("cannot fetch the resource token: unit is not the leader")
            event.set_results({"error": "cannot fetch the resource token: unit is not the leader"})
            return

        peer_relation = self.model.get_relation("livepatch")
        if not peer_relation:
            LOGGER.error("cannot fetch the resource token: peer relation not ready")
            event.set_results({"error": "cannot fetch the resource token: peer relation not ready"})
            return

        contract_token = event.params.get("contract-token", "")
        proxies = utils.get_proxy_dict(self.config)
        contracts_url = self.config.get("contracts-url", "")
        machine_token = utils.get_machine_token(contract_token, contracts_url=contracts_url, proxies=proxies)

        if not machine_token:
            LOGGER.error("failed to retrieve the machine token")
            event.set_results({"error": "cannot fetch the resource token: failed to fetch the machine token"})
            return

        resource_token = utils.get_resource_token(machine_token, contracts_url=contracts_url, proxies=proxies)

        self.set_resource_token(resource_token)
        event.set_results({"result": "resource token set"})

    def _get_db_uri(self) -> str:
        """Get connection string"""
        peer_relation = self.model.get_relation("livepatch")
        if not peer_relation:
            LOGGER.error("Failed to get peer relation")
            return None
        LOGGER.info(f"New Peer relation data = {peer_relation.data}")
        db_uri = peer_relation.data.get(self.app, {}).get("db-uri", None)
        if db_uri:
            db_uri = str(db_uri).split("?", 1)[0]
        return db_uri

    def set_status_and_log(self, msg, status) -> None:
        """
        A simple wrapper to log and set unit status simultaneously.
        """
        LOGGER.info(msg)
        self.unit.status = status(msg)


if __name__ == "__main__":
    main(LivepatchCharm, use_juju_for_storage=True)
