"""Database access helpers.

The backend talks to PostgreSQL through the DATABASE_URL environment
variable. Inside Docker Compose the hostname is the service name `db`;
from your laptop (if you ever run the API outside Docker) it would be
`localhost`.
"""

import os
import time

import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://company:company@localhost:5432/companydb",
)


def get_connection(retries: int = 10, delay: float = 1.0):
    """Return a new connection, retrying while the database finishes booting."""
    last_error = None
    for _ in range(retries):
        try:
            return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        except Exception as exc:  # deliberately broad: DB may still be starting
            last_error = exc
            time.sleep(delay)
    raise last_error
