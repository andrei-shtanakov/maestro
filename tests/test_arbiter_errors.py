"""Tests for maestro.coordination.arbiter_errors."""

import pytest

from maestro.coordination.arbiter_errors import (
    ArbiterError,
    ArbiterStartupError,
    ArbiterUnavailable,
)


def test_hierarchy() -> None:
    """Both specific errors inherit from ArbiterError."""
    assert issubclass(ArbiterStartupError, ArbiterError)
    assert issubclass(ArbiterUnavailable, ArbiterError)


def test_startup_error_carries_path_and_reason() -> None:
    err = ArbiterStartupError("binary missing", path="/nope")
    assert err.path == "/nope"
    assert "binary missing" in str(err)


def test_unavailable_carries_cause() -> None:
    original = BrokenPipeError("pipe closed")
    err = ArbiterUnavailable("arbiter subprocess died", cause=original)
    assert err.cause is original
    assert "arbiter subprocess died" in str(err)


def test_errors_can_be_raised_and_caught() -> None:
    with pytest.raises(ArbiterError):
        raise ArbiterStartupError("x")
    with pytest.raises(ArbiterError):
        raise ArbiterUnavailable("y")


def test_contract_error_is_subclass_of_arbiter_error() -> None:
    from maestro.coordination.arbiter_errors import ArbiterContractError

    assert issubclass(ArbiterContractError, ArbiterError)


def test_contract_error_sibling_of_unavailable() -> None:
    """contract_break and unavailable are sibling categories, not parent/child."""
    from maestro.coordination.arbiter_errors import ArbiterContractError

    assert not issubclass(ArbiterContractError, ArbiterUnavailable)
    assert not issubclass(ArbiterUnavailable, ArbiterContractError)


def test_contract_error_carries_code_message_data() -> None:
    from maestro.coordination.arbiter_errors import ArbiterContractError

    e = ArbiterContractError(-32602, "missing field 'agent_id'", {"field": "agent_id"})
    assert e.code == -32602
    assert e.message == "missing field 'agent_id'"
    assert e.data == {"field": "agent_id"}
    assert "-32602" in str(e)
    assert "missing field" in str(e)


def test_contract_error_data_defaults_to_empty_dict() -> None:
    from maestro.coordination.arbiter_errors import ArbiterContractError

    e = ArbiterContractError(-32603, "internal")
    assert e.data == {}
