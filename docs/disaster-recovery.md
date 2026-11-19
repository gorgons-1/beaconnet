# Backups and disaster recovery

## What is backed up

`scripts/backup.sh` runs every day at `BACKUP_TIME` from `scotland-bbs-backup.timer`. If
the Pi was off at that time, it runs as soon as the Pi is back on.

| File in `$BBS_BACKUP_DIR` | Contents |
|---|---|
| `bbs-<time>.sqlite3.gz` + `.sha256` | The database, made with SQLite's online backup API (safe while people are connected). The copy passes `integrity_check` before it is compressed, and again from the compressed file. |
| `bbs-config-<time>.tar.gz` (mode 600) | `config/`, `.env`, `secrets/` and `docker-compose.yml`. Users' public keys and account states are in the database. |
| `status.json` | The last result and last success, shown under *Backup Status* in the admin console and in `healthcheck.sh`. |

Files older than `BACKUP_KEEP_DAYS` (14) are deleted. If you set `BACKUP_REMOTE`, each
run is also copied there with rsync over SSH. Failures go to the journal
(`journalctl -t scotland-bbs-backup`), `status.json` and the admin console.

Keep at least one copy off the Pi: a USB stick, another node or `BACKUP_REMOTE`. The
config archive contains the host keys and tokens, so store it like a password.

## Restore on the same Pi

```sh
cd /opt/scotland-bbs
ls -lt /var/backups/scotland-bbs | head
sudo ./scripts/restore.sh /var/backups/scotland-bbs/bbs-<time>.sqlite3.gz
```

The script verifies the checksum and integrity first, then asks before it stops the
stack. The old database is kept in the volume as `bbs.sqlite3.pre-restore-<time>`.

## Rebuild on a replacement Pi

1. Install Raspberry Pi OS Lite 64-bit on the NVMe and give the Pi the same IP if you can.
2. Copy over the project and the two backup files (`bbs-<time>.sqlite3.gz` with its
   `.sha256`, and `bbs-config-<time>.tar.gz`).
3. `sudo ~/scotland-bbs/scripts/install.sh --import-secrets ~/bbs-config-<time>.tar.gz`
   This keeps the same host keys, so users see no "host key changed" warning, and the
   same tokens and config. No new admin is needed, because the restored database has the
   old ones.
4. `sudo /opt/scotland-bbs/scripts/restore.sh ~/bbs-<time>.sqlite3.gz`
5. `sudo /opt/scotland-bbs/scripts/healthcheck.sh`

`tests/unit/test_database.py::test_backup_restores_on_replacement_host` covers the
empty-data-directory case. Do a real restore test every quarter: run steps 2-5 on a
spare Pi or a VM.

## Database corruption

Signs: bbs-core fails its health check, the logs show `database disk image is
malformed`, or `healthcheck.sh` reports that quick_check failed.

```sh
cd /opt/scotland-bbs
docker compose exec bbs-core python -m bbs_core.cli integrity-check      # full check
sudo ./scripts/restore.sh $(ls -t /var/backups/scotland-bbs/bbs-*.sqlite3.gz | head -1)
```

Anything posted after that backup is lost. If the newest backup is too old and the
database still opens, try SQLite's `.recover` on a copy before you restore. Run it on any
machine that has the `sqlite3` CLI (`apt install sqlite3`). Never run it on the live file:

```sh
docker compose stop bbs-core
docker run --rm -v scotland-bbs_bbs-db:/data:ro -v "$PWD":/out debian:bookworm-slim \
  sh -c 'apt-get update -qq && apt-get install -y -qq sqlite3 >/dev/null && sqlite3 /data/bbs.sqlite3 ".recover" > /out/recovered.sql'
```

## Power loss

WAL mode with `synchronous=FULL` means a committed post survives a power cut. A
half-written transaction rolls back. `test_killed_writer_does_not_corrupt` covers this
with a SIGKILL mid-write. Docker starts at boot and every service has
`restart: unless-stopped`, so the BBS comes back by itself when power returns.
