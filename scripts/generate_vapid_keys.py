"""Generate one P-256 VAPID key pair for Pulse CRM Web Push."""

from __future__ import annotations

import argparse
import base64
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from email_validator import EmailNotValidError, validate_email


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _subject(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme == "mailto":
        try:
            validate_email(parsed.path, check_deliverability=False)
        except EmailNotValidError as exc:
            raise argparse.ArgumentTypeError("mailto subject must contain a valid address") from exc
        return value.strip()
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise argparse.ArgumentTypeError("subject must be a valid mailto: or https:// URI")
    return value.strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate VAPID environment values. Keep the private key secret.",
    )
    parser.add_argument(
        "--subject",
        required=True,
        type=_subject,
        help="VAPID contact URI, for example mailto:admin@example.com",
    )
    args = parser.parse_args()

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_value = private_key.public_key().public_bytes(
        Encoding.X962,
        PublicFormat.UncompressedPoint,
    )

    print(f"PULSE_WEB_PUSH_VAPID_PUBLIC_KEY={_base64url(public_value)}")
    print(f"PULSE_WEB_PUSH_VAPID_PRIVATE_KEY={_base64url(private_value)}")
    print(f"PULSE_WEB_PUSH_VAPID_SUBJECT={args.subject}")


if __name__ == "__main__":
    main()
