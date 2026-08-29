from __future__ import annotations

import pytest

from app.integrations.identity import (
    IdentityNormalizationError,
    normalize_email_address,
    normalize_phone_number,
)


def test_identity_normalization() -> None:
    assert normalize_email_address(" Anna@Example.COM ") == "anna@example.com"
    assert normalize_phone_number("8 (000) 000-00-01") == "+70000000001"
    assert normalize_phone_number("+44 20 7946 0958") == "+442079460958"
    with pytest.raises(IdentityNormalizationError):
        normalize_phone_number("123")
