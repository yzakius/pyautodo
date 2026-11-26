import importlib.util
import socket
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest


PROJECT_FILE = Path(__file__).resolve().parents[1] / "pyautodo.py"


def load_script(
    monkeypatch,
    *,
    droplets=None,
    skip_list="",
    ssh_ports="",
    configure_client=None,
):
    """Load the script with every external integration replaced by a fake."""
    client = MagicMock(name="digitalocean_client")
    client.droplets.list.return_value = {"droplets": droplets or []}
    if configure_client is not None:
        configure_client(client)
    client_factory = MagicMock(name="Client", return_value=client)
    dotenv_loader = MagicMock(name="load_dotenv")

    dotenv_module = types.ModuleType("dotenv")
    dotenv_module.load_dotenv = dotenv_loader

    pydo_module = types.ModuleType("pydo")
    pydo_module.Client = client_factory

    azure_module = types.ModuleType("azure")
    azure_core_module = types.ModuleType("azure.core")
    azure_exceptions_module = types.ModuleType("azure.core.exceptions")

    class FakeClientAuthenticationError(Exception):
        pass

    azure_exceptions_module.ClientAuthenticationError = FakeClientAuthenticationError
    azure_module.core = azure_core_module
    azure_core_module.exceptions = azure_exceptions_module

    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "pydo", pydo_module)
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core_module)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_exceptions_module)
    monkeypatch.setenv("DO_TOKEN", "token-for-tests")
    monkeypatch.setenv("SKIP_LIST", skip_list)
    monkeypatch.setenv("SSH_PORTS", ssh_ports)

    spec = importlib.util.spec_from_file_location("pyautodo_under_test", PROJECT_FILE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, client, client_factory, dotenv_loader


@pytest.fixture
def loaded_script(monkeypatch):
    return load_script(monkeypatch)


def test_initialization_uses_environment_token(loaded_script):
    module, client, client_factory, dotenv_loader = loaded_script

    dotenv_loader.assert_called_once_with()
    client_factory.assert_called_once_with("token-for-tests")
    client.droplets.list.assert_called_once_with()
    assert module.skip_list == []
    assert module.ssh_ports == {}


def test_ssh_ports_are_parsed_from_environment(monkeypatch):
    module, _, _, _ = load_script(monkeypatch, ssh_ports=" web-01:2222, db-01:2200 ")

    assert module.ssh_ports == {"web-01": 2222, "db-01": 2200}


def test_get_droplet_status_returns_status(loaded_script):
    module, client, _, _ = loaded_script
    client.droplets.get.return_value = {"droplet": {"status": "active"}}

    status = module.get_droplet_status(droplet_id=123)

    assert status == "active"
    client.droplets.get.assert_called_once_with(droplet_id=123)


@pytest.mark.parametrize(
    ("api_response", "expected"),
    [
        (
            {"meta": {"total": 2}, "snapshots": [{"id": 91}, {"id": 90}]},
            {"total": 2, "id": 91},
        ),
        ({"meta": {"total": 0}, "snapshots": []}, {"total": 0, "id": None}),
    ],
)
def test_get_droplets_snapshot_list(loaded_script, api_response, expected):
    module, client, _, _ = loaded_script
    client.droplets.list_snapshots.return_value = api_response

    result = module.get_droplets_snapshot_list(droplet_id=123)

    assert result == expected
    client.droplets.list_snapshots.assert_called_once_with(123)


def test_create_snapshot_waits_until_action_is_completed(loaded_script, monkeypatch):
    module, client, _, _ = loaded_script
    client.droplet_actions.post.return_value = {"action": {"id": 456}}
    client.droplet_actions.get.side_effect = [
        {"action": {"status": "in-progress"}},
        {"action": {"status": "completed"}},
    ]
    sleep = MagicMock(name="sleep")
    monkeypatch.setattr(module.time, "sleep", sleep)

    class FixedDatetime:
        @classmethod
        def now(cls):
            return cls()

        def strftime(self, format_string):
            assert format_string == "%Y-%m"
            return "2026-08"

    monkeypatch.setattr(module, "datetime", FixedDatetime)

    created = module.create_droplet_snapshot(
        droplet_id=123,
        droplet_name="web-01",
    )

    assert created is True
    client.droplet_actions.post.assert_called_once_with(
        123,
        body={"type": "snapshot", "name": "2026-08-web-01"},
    )
    assert client.droplet_actions.get.call_args_list == [
        call(droplet_id=123, action_id=456),
        call(droplet_id=123, action_id=456),
    ]
    sleep.assert_called_once_with(30)


def test_delete_snapshot_waits_for_only_the_new_snapshot(
    loaded_script,
    monkeypatch,
):
    module, client, _, _ = loaded_script
    snapshot_list = MagicMock(return_value={"total": 1, "id": 222})
    sleep = MagicMock(name="sleep")
    monkeypatch.setattr(module, "get_droplets_snapshot_list", snapshot_list)
    monkeypatch.setattr(module.time, "sleep", sleep)

    module.delete_droplet_snapshot(snapshot_id=111, droplet_id=123)

    client.snapshots.delete.assert_called_once_with(snapshot_id=111)
    assert snapshot_list.call_args_list == [
        call(droplet_id=123),
        call(droplet_id=123),
    ]
    sleep.assert_called_once_with(18)


def test_power_on_retries_until_ssh_is_available(loaded_script, monkeypatch):
    module, client, _, _ = loaded_script
    connection = MagicMock(name="ssh_connection")
    create_connection = MagicMock(
        side_effect=[OSError("not ready"), connection],
    )
    sleep = MagicMock(name="sleep")
    monkeypatch.setattr(module.socket, "create_connection", create_connection)
    monkeypatch.setattr(module.time, "sleep", sleep)

    module.power_on_droplet(droplet_id=123, droplet_ip="203.0.113.10")

    client.droplet_actions.post.assert_called_once_with(
        droplet_id=123,
        body={"type": "power_on"},
    )
    assert create_connection.call_args_list == [
        call(("203.0.113.10", 22), timeout=5),
        call(("203.0.113.10", 22), timeout=5),
    ]
    sleep.assert_called_once_with(20)
    connection.close.assert_called_once_with()


def test_power_on_uses_custom_port(loaded_script, monkeypatch):
    module, _, _, _ = loaded_script
    create_connection = MagicMock(return_value=MagicMock(name="ssh_connection"))
    monkeypatch.setattr(module.socket, "create_connection", create_connection)

    module.power_on_droplet(droplet_id=123, droplet_ip="203.0.113.10", port=2222)

    create_connection.assert_called_once_with(("203.0.113.10", 2222), timeout=5)


def test_power_off_when_droplet_is_already_off(loaded_script, monkeypatch):
    module, client, _, _ = loaded_script
    get_status = MagicMock(return_value="off")
    sleep = MagicMock(name="sleep")
    monkeypatch.setattr(module, "get_droplet_status", get_status)
    monkeypatch.setattr(module.time, "sleep", sleep)

    module.power_off_droplet(droplet_id=123)

    client.droplet_actions.post.assert_called_once_with(
        droplet_id=123,
        body={"type": "power_off"},
    )
    get_status.assert_called_once_with(droplet_id=123)
    sleep.assert_not_called()


def test_power_off_waits_until_droplet_is_off(loaded_script, monkeypatch):
    module, _, _, _ = loaded_script
    get_status = MagicMock(side_effect=["active", "off"])
    sleep = MagicMock(name="sleep")
    monkeypatch.setattr(module, "get_droplet_status", get_status)
    monkeypatch.setattr(module.time, "sleep", sleep)

    module.power_off_droplet(droplet_id=123)

    assert get_status.call_count == 2
    sleep.assert_called_once_with(5)


def test_droplet_in_skip_list_is_not_modified(monkeypatch):
    droplet = {
        "id": 123,
        "name": "database-01",
        "networks": {"v4": []},
    }

    module, client, _, _ = load_script(
        monkeypatch,
        droplets=[droplet],
        skip_list=" database-01, another-droplet ",
    )

    assert module.skip_list == ["database-01", "another-droplet"]
    client.droplets.list_snapshots.assert_not_called()
    client.droplet_actions.post.assert_not_called()
    client.snapshots.delete.assert_not_called()


def test_droplet_without_public_ipv4_is_not_modified(monkeypatch):
    droplet = {
        "id": 123,
        "name": "private-01",
        "networks": {"v4": [{"type": "private", "ip_address": "10.0.0.10"}]},
    }

    _, client, _, _ = load_script(monkeypatch, droplets=[droplet])

    client.droplets.list_snapshots.assert_not_called()
    client.droplet_actions.post.assert_not_called()
    client.snapshots.delete.assert_not_called()


@pytest.mark.parametrize(
    ("ssh_ports", "expected_port"),
    [("", 22), ("web-01:2222", 2222), ("other-01:2222", 22)],
)
def test_droplet_is_powered_on_when_snapshot_creation_fails(
    monkeypatch,
    ssh_ports,
    expected_port,
):
    droplet = {
        "id": 123,
        "name": "web-01",
        "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.10"}]},
    }
    connection = MagicMock(name="ssh_connection")
    configured_clients = []

    def configure_client(client):
        configured_clients.append(client)
        client.droplets.list_snapshots.return_value = {
            "meta": {"total": 1},
            "snapshots": [{"id": 111}],
        }
        client.droplets.get.return_value = {"droplet": {"status": "off"}}
        client.droplet_actions.post.side_effect = [
            {"action": {"id": 1}},
            RuntimeError("snapshot failed"),
            {"action": {"id": 2}},
        ]

    monkeypatch.setattr(socket, "create_connection", MagicMock(return_value=connection))

    with pytest.raises(RuntimeError, match="snapshot failed"):
        load_script(
            monkeypatch,
            droplets=[droplet],
            ssh_ports=ssh_ports,
            configure_client=configure_client,
        )

    client = configured_clients[0]
    assert client.droplet_actions.post.call_args_list[-1] == call(
        droplet_id=123,
        body={"type": "power_on"},
    )
    socket.create_connection.assert_called_once_with(
        ("203.0.113.10", expected_port),
        timeout=5,
    )
    connection.close.assert_called_once_with()
