# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utils module."""

import csv
import json
import os
import subprocess
import typing as t

import requests

DEFAULT_CONTRACTS_URL = "https://contracts.canonical.com"
RESOURCE_NAME = "livepatch-onprem"


def map_config_to_env_vars(charm, **additional_env):
    """
    Map the config values provided in config.yaml into environment variables.

    After that, the vars can be passed directly to the pebble layer.
    Variables must match the form LP_<Key1>_<key2>_<key3>...
    """
    env_mapped_config = {"LP_" + k.replace("-", "_").replace(".", "_").upper(): v for k, v in charm.config.items()}

    env_mapped_config["LP_SERVER_IS_LEADER"] = charm.unit.is_leader()

    return {**env_mapped_config, **additional_env}


def get_proxy_dict(cfg) -> t.Optional[dict]:
    """Generate an http proxy server configuration dictionary."""
    d = {
        "http_proxy": cfg.get("http_proxy", "") or os.environ.get("JUJU_CHARM_HTTP_PROXY", ""),
        "https_proxy": cfg.get("https_proxy", "") or os.environ.get("JUJU_CHARM_HTTPS_PROXY", ""),
        "no_proxy": cfg.get("no_proxy", "") or os.environ.get("JUJU_CHARM_NO_PROXY", ""),
    }
    if all(v == "" for v in d.values()):
        return None
    return d


def get_machine_token(contract_token: str, contracts_url=DEFAULT_CONTRACTS_URL, proxies=None) -> t.Optional[str]:
    """Retrieve a resource token for the livepatch-onprem resource."""
    if proxies is not None:
        os.environ["http_proxy"] = proxies.get("http_proxy", "")
        os.environ["https_proxy"] = proxies.get("https_proxy", "")
        os.environ["no_proxy"] = proxies.get("no_proxy", "")

    system_information = get_system_information()
    payload = {
        "architecture": system_information.get("architecture", ""),
        "hostType": "container",
        "machineId": "livepatch-onprem",
        "os": {
            "distribution": system_information.get("version", ""),
            "kernel": system_information.get("kernel-version", ""),
            "release": system_information.get("version_id", ""),
            "series": system_information.get("version_codename", ""),
            "type": "Linux",
        },
    }

    headers = {
        "Authorization": f"Bearer {contract_token}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            url=f"{contracts_url}/v1/context/machines/token",
            data=json.dumps(payload),
            headers=headers,
        )
        data = response.json()
        return data.get("machineToken", "")
    except requests.exceptions.RequestException:
        return None
    except KeyError:
        return None


def get_resource_token(machine_token, contracts_url=DEFAULT_CONTRACTS_URL, proxies=None):
    """Retrieve a resource token for the livepatch-onprem resource."""
    if proxies is not None:
        os.environ["http_proxy"] = proxies.get("http_proxy", "")
        os.environ["https_proxy"] = proxies.get("https_proxy", "")
        os.environ["no_proxy"] = proxies.get("no_proxy", "")

    headers = {"Authorization": f"Bearer {machine_token}"}
    try:
        req = requests.get(
            url=f"{contracts_url}/v1/resources/{RESOURCE_NAME}/context/machines/livepatch-onprem",
            headers=headers,
        )
        data = req.json()
        return data.get("resourceToken", "")
    except requests.exceptions.RequestException:
        return None
    except KeyError:
        return None


def get_system_information() -> dict:
    """Fetch system information: kernel version, architecture, os, etc."""
    system_information = {}
    with open("/etc/os-release") as f:
        reader = csv.reader(f, delimiter="=")
        for row in reader:
            if row:
                system_information[row[0].lower()] = row[1]
    system_information["kernel-version"] = run_uname("-r")
    system_information["architecture"] = run_uname("-m")
    return system_information


def run_uname(flag) -> str:
    """Run uname command with the given flag."""
    result = subprocess.check_output(["uname", flag])
    return result.decode("utf-8").strip()
