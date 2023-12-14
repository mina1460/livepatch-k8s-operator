# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.


import logging

LOGGER = logging.getLogger(__name__)
WORKLOAD_CONTAINER = "livepatch"
SCHEMA_UPGRADE_CONTAINER = "livepatch-schema-upgrade"


class PgIsReadyStates:
    CONNECTED = 0
    REJECTED = 1
    NO_RESPONSE = 2
    NO_ATTEMPT = 3
