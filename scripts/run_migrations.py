"""Run Alembic under a PostgreSQL advisory lock.

Every application instance uses the same lock key, so a later deployment can
safely overlap an older container during startup without running a migration
twice. The migration itself still opens its regular SQLAlchemy connection.
"""

from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import create_engine, text


LOCK_KEY = 7_035_467_038_742_651_345


def main() -> None:
    database_url = os.environ.get("PULSE_DATABASE_URL")
    if not database_url:
        raise SystemExit("PULSE_DATABASE_URL is required to run migrations")

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    "/app/backend/alembic.ini",
                    "upgrade",
                    "head",
                ],
                cwd="/app/backend",
                check=True,
            )
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
            )
    engine.dispose()


if __name__ == "__main__":
    main()
