"""Settings.backend_max_retries — opt-in transient-backend retry count,
distinct from the gate/evaluation max_retries."""
import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Settings


def test_backend_max_retries_defaults_to_zero():
    """Default 0 = today's behavior (one attempt, terminal)."""
    assert Settings().backend_max_retries == 0


def test_backend_max_retries_is_independent_of_max_retries():
    """The two budgets are separate fields — setting one leaves the other."""
    s = Settings(backend_max_retries=3)
    assert s.backend_max_retries == 3
    assert s.max_retries == 3  # gate default, untouched

    s2 = Settings(max_retries=7)
    assert s2.max_retries == 7
    assert s2.backend_max_retries == 0  # backend default, untouched


def test_backend_max_retries_accepts_positive_int():
    assert Settings(backend_max_retries=5).backend_max_retries == 5


def test_unknown_field_still_rejected_under_extra_forbid():
    """extra='forbid' invariant: a typo'd field is a hard error."""
    with pytest.raises(ValidationError):
        Settings(backend_max_retry=2)  # singular typo
