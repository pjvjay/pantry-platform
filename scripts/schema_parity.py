#!/usr/bin/env python3
"""Compare the schema pantry-db's migrations build with the one pantry-api's models build.

    python scripts/schema_parity.py --mig URL --orm URL [--allow FILE] [--verbose]

Production's tables come from pantry-db's SQL migrations (the migrate job).
pantry-api's tests build theirs from its SQLAlchemy models with create_all, on
SQLite and on Postgres. Nothing else compares the two, so a model that makes a
column nullable while the migration says NOT NULL passes every test, and the
first insert of a NULL to fail is one in production.

The CI job builds two empty databases on one Postgres server: `mig` with
pantry-db's run-migrations.sh, and `orm` with
pantry_planner.db.Base.metadata.create_all. This script reflects both with
sqlalchemy.inspect and compares, table by table:

  - which tables and columns exist;
  - column types, normalised so the spellings that mean the same thing agree
    (String and text, Float and double precision, Integer and integer);
  - nullability;
  - server defaults (a text default reads the same as a varchar one, and an
    identity or SERIAL column reads as `identity` or `serial`);
  - primary keys, foreign keys and CHECK constraints.

Indexes and unique constraints are not compared.

Each difference prints as one line, and that exact line is what the allowlist
(scripts/schema_parity_allow.json) lists, in a group with the reason it is
acceptable. A difference that is not listed fails the run (exit 1). A listed
difference that is not found is printed but does not fail: CI checks out the
platform's pins, which can be older or newer than the branch the entry was
written for.

Needs sqlalchemy and psycopg, which pantry-api's install brings.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

DEFAULT_ALLOW = Path(__file__).resolve().parent / "schema_parity_allow.json"

# The model's String is varchar with no length, which Postgres treats exactly
# like text; Float builds double precision. Anything else keeps its own name,
# so real against double precision, or varchar(20) against text, still shows.
_SAME_TYPE = {
    "TEXT": "text",
    "VARCHAR": "text",
    "DOUBLE PRECISION": "double precision",
    "FLOAT": "double precision",
    "INTEGER": "integer",
}

# Postgres writes a default or a CHECK on a text column as ''::text and on a
# varchar column as ''::character varying. Since the two types count as the
# same, so do the casts.
_TEXT_CAST = re.compile(r"::(?:text|character varying)\b")

_NEXTVAL = re.compile(r"^nextval\('(?P<seq>[^']+)'::regclass\)$")


@dataclass
class Table:
    columns: dict[str, dict[str, str]]  # name -> {type, nullable, default}, in table order
    primary_key: tuple[str, ...]
    foreign_keys: set[str]
    checks: set[str]


def norm_type(type_: sa.types.TypeEngine) -> str:
    compiled = type_.compile(dialect=postgresql.dialect())
    return _SAME_TYPE.get(compiled, compiled.lower())


def norm_default(table: str, column: dict) -> str:
    identity = column.get("identity")
    if identity:
        return "identity always" if identity.get("always") else "identity"
    default = column.get("default")
    if default is None:
        return "none"
    # create_all builds a lone Integer primary key as SERIAL, whose default
    # is nextval() on a sequence named after the column. Calling it `serial`
    # keeps the generated sequence name out of the allowlist.
    m = _NEXTVAL.match(default)
    if m and m["seq"] == f"{table}_{column['name']}_seq":
        return "serial"
    return _TEXT_CAST.sub("", default)


def _cols(names: list[str] | tuple[str, ...]) -> str:
    return "(" + ", ".join(names) + ")"


def norm_foreign_key(fk: dict) -> str:
    text = (f"{_cols(fk['constrained_columns'])} -> "
            f"{fk['referred_table']} {_cols(fk['referred_columns'])}")
    for option in ("ondelete", "onupdate"):
        value = (fk.get("options") or {}).get(option)
        if value:
            text += f" on {option[2:]} {value.lower()}"
    return text


def snapshot(url: str) -> dict[str, Table]:
    engine = sa.create_engine(url)
    try:
        insp = sa.inspect(engine)
        tables = {}
        for name in insp.get_table_names():
            tables[name] = Table(
                columns={
                    c["name"]: {
                        "type": norm_type(c["type"]),
                        "nullable": "yes" if c["nullable"] else "no",
                        "default": norm_default(name, c),
                    }
                    for c in insp.get_columns(name)
                },
                primary_key=tuple(insp.get_pk_constraint(name)["constrained_columns"]),
                foreign_keys={norm_foreign_key(fk) for fk in insp.get_foreign_keys(name)},
                checks={_TEXT_CAST.sub("", c["sqltext"]) for c in insp.get_check_constraints(name)},
            )
        return tables
    finally:
        engine.dispose()


def diff(mig: dict[str, Table], orm: dict[str, Table]) -> list[str]:
    """Every difference between the two schemas, one line each, in a stable order."""
    out: list[str] = []
    for name in sorted(mig.keys() | orm.keys()):
        if name not in orm:
            out.append(f"table {name}: only in mig")
            continue
        if name not in mig:
            out.append(f"table {name}: only in orm")
            continue
        m, o = mig[name], orm[name]
        for col in [*m.columns, *(c for c in o.columns if c not in m.columns)]:
            if col not in o.columns:
                out.append(f"column {name}.{col}: only in mig")
                continue
            if col not in m.columns:
                out.append(f"column {name}.{col}: only in orm")
                continue
            for aspect in ("type", "nullable", "default"):
                mv, ov = m.columns[col][aspect], o.columns[col][aspect]
                if mv != ov:
                    out.append(f"column {name}.{col}: {aspect} mig {mv}, orm {ov}")
        if m.primary_key != o.primary_key:
            out.append(
                f"table {name}: primary key mig {_cols(m.primary_key)}, orm {_cols(o.primary_key)}"
            )
        for side, mine, theirs in (("mig", m, o), ("orm", o, m)):
            for fk in sorted(mine.foreign_keys - theirs.foreign_keys):
                out.append(f"table {name}: foreign key {fk}: only in {side}")
            for check in sorted(mine.checks - theirs.checks):
                out.append(f"table {name}: check {check}: only in {side}")
    return out


def load_allow(path: Path) -> dict[str, str]:
    """Allowed difference -> the reason it is allowed."""
    groups = json.loads(path.read_text())["allowed"]
    allowed: dict[str, str] = {}
    for i, group in enumerate(groups, 1):
        reason = group.get("reason", "").strip()
        if not reason or not group.get("diffs"):
            raise SystemExit(f"{path}: group {i} needs a reason and at least one diff")
        for line in group["diffs"]:
            if line in allowed:
                raise SystemExit(f"{path}: listed twice: {line}")
            allowed[line] = reason
    return allowed


def _masked(url: str) -> str:
    return sa.engine.make_url(url).render_as_string(hide_password=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mig", required=True,
                        help="SQLAlchemy URL of the database the migrations built")
    parser.add_argument("--orm", required=True,
                        help="SQLAlchemy URL of the database create_all built")
    parser.add_argument("--allow", type=Path, default=DEFAULT_ALLOW, help="allowlist (JSON)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print every allowed difference with its reason")
    args = parser.parse_args(argv)

    allowed = load_allow(args.allow)
    mig, orm = snapshot(args.mig), snapshot(args.orm)
    found = diff(mig, orm)
    not_allowed = [line for line in found if line not in allowed]
    not_found = [line for line in allowed if line not in found]

    print(f"mig: {_masked(args.mig)}, {len(mig)} tables")
    print(f"orm: {_masked(args.orm)}, {len(orm)} tables")
    print(f"{len(found)} differences: {len(found) - len(not_allowed)} allowed, "
          f"{len(not_allowed)} not allowed")
    if args.verbose:
        by_reason: dict[str, list[str]] = {}
        for line in found:
            if line in allowed:
                by_reason.setdefault(allowed[line], []).append(line)
        for reason, lines in by_reason.items():
            print(f"\nAllowed: {reason}")
            for line in lines:
                print(f"  {line}")
    if not_found:
        print(f"\n{len(not_found)} allowed differences not found here (fixed since, or for "
              "tables these checkouts do not have):")
        for line in not_found:
            print(f"  {line}")
    if not_allowed:
        print("\nNot allowed. Change the model or add a migration so the two agree; or, if the "
              f"difference is intended, add the line to {args.allow.name} with the reason:")
        for line in not_allowed:
            print(f"  {json.dumps(line)},")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            for line in not_allowed:
                print(f"::error title=schema parity::{line}")
        return 1
    print("schema parity ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
