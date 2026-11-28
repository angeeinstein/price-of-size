"""Database backups with pg_dump (custom format), e.g. before every migration.

The PostgreSQL client in the coordinator image matches the server's major
version (see deploy/docker/coordinator.Dockerfile).
"""

import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)


def libpq_url(sqlalchemy_url: str) -> str:
    """postgresql+psycopg://... -> postgresql://... (what pg_dump understands)."""
    parts = urlsplit(sqlalchemy_url)
    scheme = parts.scheme.split("+", 1)[0]
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def libpq_env(sqlalchemy_url: str) -> dict[str, str]:
    """Connection parameters as PG* environment variables."""
    from urllib.parse import parse_qs, unquote

    parts = urlsplit(libpq_url(sqlalchemy_url))
    env = {
        "PGHOST": parts.hostname or "localhost",
        "PGPORT": str(parts.port or 5432),
        "PGDATABASE": parts.path.lstrip("/"),
    }
    if parts.username:
        env["PGUSER"] = unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    sslmode = parse_qs(parts.query).get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode[0]
    return env


def run_backup(database_url: str, out_dir: Path, label: str, keep: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = out_dir / f"pos-{stamp}-{label}.dump"
    tmp = final.with_suffix(".dump.partial")
    subprocess.run(
        ["pg_dump", "--format=custom", "--compress=6", f"--file={tmp}"],
        check=True,
        env={**os.environ, **libpq_env(database_url)},  # password never appears in `ps`
    )
    if tmp.stat().st_size == 0:
        raise RuntimeError("pg_dump produced an empty file")
    os.replace(tmp, final)
    log.info("backup written: %s (%d bytes)", final, final.stat().st_size)
    _prune(out_dir, keep, label)
    return final


def _prune(out_dir: Path, keep: int, label: str) -> None:
    """The newest `keep` dumps of the same kind stay: dumps before updates
    ("before-<version>") count together, any other label on its own."""
    pattern = "pos-*-before-*.dump" if label.startswith("before-") else f"pos-*-{label}.dump"
    dumps = sorted(out_dir.glob(pattern))
    for old in dumps[: max(len(dumps) - keep, 0)]:
        old.unlink()
        log.info("pruned old backup %s", old.name)
