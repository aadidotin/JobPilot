"""Nightly encrypted backup (eng T5, CEO T4).

The database holds third-party PII and, more importantly, holds data that
cannot be regenerated: your 👍/👎 annotations are judgment calls, not
something a re-poll recreates. Losing them costs the weekend-1 measurement
period and the golden set weekend 2 trains on.

Three properties this needs and a `cp` does not:

- Consistent while the bot daemon is connected. sqlite3's online backup API
  takes a proper snapshot of a live WAL database; copying the file can catch
  it mid-transaction with its -wal alongside.
- Encrypted at rest (CEO T4), so a backup that ends up on cloud sync or an
  external disk is not a plaintext dump of third-party contact data.
- Verified. `restore` is exercised by the tests, because an unrestorable
  backup is just a file that makes you feel safe.

Threat model, stated honestly: the passphrase lives in a 0600 file in your
home directory, so this protects backups that LEAVE the machine. It does not
protect against someone who already has your home directory.
"""

import gzip
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("jobpilot.backup")

KEEP_DAYS = 14
KEY_PATH = Path(os.environ.get("JOBPILOT_BACKUP_KEY", Path.home() / ".config/jobpilot/backup.key"))
DEFAULT_DIR = Path(os.environ.get("JOBPILOT_BACKUP_DIR", Path.home() / ".local/share/jobpilot/backups"))


def ensure_key(key_path: Path = KEY_PATH) -> Path:
    """A random passphrase, generated once, readable only by the owner."""
    if key_path.exists():
        return key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(secrets.token_urlsafe(48) + "\n")
    key_path.chmod(0o600)
    log.warning("generated a new backup key at %s — back THIS up somewhere else, "
                "or the encrypted backups are unreadable", key_path)
    return key_path


def snapshot(db_path: str | Path, dest: Path) -> Path:
    """Consistent copy of a live database via the sqlite3 backup API."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def _gpg(args: list[str], key_path: Path, stdin_path: Path, stdout_path: Path) -> None:
    with open(stdin_path, "rb") as fin, open(stdout_path, "wb") as fout:
        proc = subprocess.run(
            ["gpg", "--batch", "--yes", "--quiet", "--passphrase-file", str(key_path),
             "--pinentry-mode", "loopback", *args],
            stdin=fin, stdout=fout, stderr=subprocess.PIPE,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"gpg failed: {proc.stderr.decode()[:300]}")


def backup(db_path: str | Path, dest_dir: Path | None = None, now: datetime | None = None,
           key_path: Path | None = None, keep_days: int = KEEP_DAYS) -> Path:
    """Snapshot -> gzip -> gpg symmetric. Returns the encrypted artifact."""
    dest_dir = Path(dest_dir or DEFAULT_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    key_path = ensure_key(key_path or KEY_PATH)
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    final = dest_dir / f"jobpilot-{stamp}.db.gz.gpg"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw, zipped = tmp / "snap.db", tmp / "snap.db.gz"
        snapshot(db_path, raw)
        with open(raw, "rb") as fin, gzip.open(zipped, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        _gpg(["--symmetric", "--cipher-algo", "AES256"], key_path, zipped, final)

    final.chmod(0o600)
    prune(dest_dir, keep_days, now)
    return final


def restore(archive: Path, dest: Path, key_path: Path | None = None) -> Path:
    """Inverse of backup(). Exercised by the tests on purpose."""
    key_path = key_path or KEY_PATH
    with tempfile.TemporaryDirectory() as tmp:
        zipped = Path(tmp) / "restore.db.gz"
        _gpg(["--decrypt"], key_path, archive, zipped)
        with gzip.open(zipped, "rb") as fin, open(dest, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    return dest


def prune(dest_dir: Path, keep_days: int = KEEP_DAYS, now: datetime | None = None) -> int:
    """Keep the newest `keep_days` archives. Count-based rather than
    mtime-based: a laptop that was off for a week must not wake up and delete
    every backup it has."""
    archives = sorted(dest_dir.glob("jobpilot-*.db.gz.gpg"))
    doomed = archives[:-keep_days] if keep_days > 0 else []
    for path in doomed:
        path.unlink()
    return len(doomed)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from jobpilot.db import DB_PATH

    if not Path(DB_PATH).exists():
        log.error("no database at %s", DB_PATH)
        return 1
    try:
        path = backup(DB_PATH)
    except Exception:
        log.exception("backup failed")
        return 1
    log.info("backup written: %s (%.1f KB)", path, path.stat().st_size / 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
