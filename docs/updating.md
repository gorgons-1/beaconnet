# Updating

Each release is a git tag (`v1.2.0`) in the GitHub repository. A node installed from a
git clone updates itself from there.

## Update by hand

```sh
cd /opt/scotland-bbs
sudo ./scripts/update.sh --check     # is there a new release? (exit code 10 = yes)
sudo ./scripts/update.sh             # show the changes, ask, then update
```

An update:

1. Fetches the release tags from GitHub and shows the commits since your version.
2. Takes a verified backup (database, config, `.env`, secrets).
3. Checks out the new release.
4. Runs `install.sh` from it, which rebuilds the images, applies database migrations,
   adds any new `.env` settings with their defaults, restarts and re-runs the checks.

Users are disconnected for about a minute while the containers restart. Mail and
bulletins queued for other nodes stay in the database and go out once bbs-sync is back.

Never touched by an update: `.env`, `config/`, `secrets/`, the database volume and
`/var/backups/scotland-bbs`. Git doesn't track any of them.

## Automatic checks

`install.sh` enables `scotland-bbs-update-check.timer`, which runs once a week. It runs
`update.sh --auto`:

* By default it only **checks**. If a new release exists, it appears under **System
  health** in the admin console.
* With `BBS_AUTO_UPDATE=true` in `.env`, it also installs the release, with a backup
  first. On a node people rely on, leave this off and update when you
  choose.

```sh
systemctl list-timers scotland-bbs-update-check.timer
journalctl -u scotland-bbs-update-check.service
```

## A specific version, or rolling back

```sh
sudo ./scripts/update.sh --to v1.1.0
```

Database migrations only go forward. If the release you are leaving changed the schema,
also restore the backup taken just before the update:

```sh
ls /var/backups/scotland-bbs/
sudo ./scripts/restore.sh /var/backups/scotland-bbs/bbs-<time>.sqlite3.gz
```

## Nodes installed without git

A node that was copied over with rsync, rather than cloned, can't fetch updates. Switch
it to git once, and its data stays where it is:

```sh
sudo apt-get install -y git
git clone https://github.com/gorgons-1/beaconnet.git ~/scotland-bbs
sudo ~/scotland-bbs/scripts/install.sh
```

The installer copies the checkout, `.git` included, over `/opt/scotland-bbs`. It keeps
your `.env`, `config/` and secrets. From then on `update.sh` works. You can also copy in
new code by hand and run `sudo ./scripts/update.sh --local`.

## For maintainers: making a release

```sh
# bump VERSION and add a CHANGELOG.md entry, commit, then:
git tag -a v1.2.0 -m "v1.2.0"
git push origin main --tags
```

Nodes pick up the new tag at their next check. Every release must pass the test workflow
on GitHub (`.github/workflows/tests.yml`). Add a new database change as a new numbered
file in `migrations/`, and never edit one that has already been released.
