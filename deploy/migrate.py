"""pantry-db's migrations and seed, applied the way pantry-db/scripts/run-migrations.sh applies
them, for a host with no psql (Render's pre-deploy step runs in the API image).

Same contract as the shell runner: a schema_migrations table; every /migrations/*.sql not in it
applied in name order and recorded; then /seeds/seed.sql, which is idempotent and brings its own
transaction, every time. Each migration runs in one transaction with its record, so a failed one
leaves nothing half applied and the next deploy tries it again. Reads DB_HOST, DB_PORT, DB_NAME, DB_USER and
DB_PASSWORD, as the shell runner does.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psycopg

MIGRATIONS = Path(os.environ.get("MIGRATIONS_DIR", "/migrations"))
SEED = Path(os.environ.get("SEED_FILE", "/seeds/seed.sql"))


def connect() -> psycopg.Connection:
    need = [k for k in ("DB_HOST", "DB_USER", "DB_PASSWORD") if not os.environ.get(k)]
    if need:
        sys.exit(f"{', '.join(need)} required")
    args = {"host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
            "dbname": os.environ.get("DB_NAME", "pantry"), "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"], "connect_timeout": 5}
    for attempt in range(30):
        try:
            return psycopg.connect(**args, autocommit=True)
        except psycopg.OperationalError as e:
            print(f"waiting for Postgres at {args['host']}:{args['port']} ({e.__class__.__name__})",
                  flush=True)
            time.sleep(5)
    sys.exit("Postgres not ready after 150s, giving up")


def main() -> int:
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     " version text PRIMARY KEY,"
                     " applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.stem in done:
                print(f"skip  {f.stem} (already applied)", flush=True)
                continue
            print(f"apply {f.stem}", flush=True)
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
        # seed.sql brings its own BEGIN and COMMIT, so it runs outside any transaction of ours
        print("seed  seeds/seed.sql (idempotent)", flush=True)
        conn.execute(SEED.read_text(encoding="utf-8"))
        for v, at in conn.execute("SELECT version, applied_at FROM schema_migrations "
                                  "ORDER BY version"):
            print(f"      {v}  {at:%Y-%m-%d %H:%M}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
