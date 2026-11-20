#!/usr/bin/env python3
"""BeaconNet Desktop: notifications and a browser view for a BeaconNet BBS node.

Runs on your own computer (Windows, macOS or Linux). It keeps one encrypted SSH
connection to your BBS node, checks for news every 30 seconds, pops up desktop
notifications, and serves a page at http://127.0.0.1:8765 where you can read, post,
send mail and - if you are a sysop on the node's LAN - approve registrations.

    python beaconnet_desktop.py            # start, and open the page in your browser
    python beaconnet_desktop.py --no-browser --port 8765

Needs Python 3.9+ and the 'asyncssh' package (the launchers install it for you).
Nothing secret is written to disk except, if you choose "remember this computer", a
private SSH key for this computer only (in your user profile, readable only by you).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import ssl
import tarfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

try:
    import asyncssh
except ImportError:  # pragma: no cover - the launchers install it
    sys.exit("BeaconNet Desktop needs the 'asyncssh' package:  python -m pip install --user asyncssh")

VERSION = "1.4.4"
SUBSYSTEM = "beaconnet-client"
POLL_SECONDS = 30
INFO_REFRESH_CHECKS = 10            # re-read node name/id every ~5 minutes
# Updates: each GitHub release carries BeaconNet-version.txt (this app's version in that
# release), attached only after all three downloads are, so "newer" always means "ready".
RELEASES = "https://github.com/gorgons-1/beaconnet/releases/latest"
UPDATE_CHECK_HOURS = 12
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def platform_asset() -> str:
    if sys.platform == "win32":
        return "BeaconNet-Windows.zip"
    if sys.platform == "darwin":
        return "BeaconNet-macOS.zip"
    return "BeaconNet-Linux-x86_64.tar.gz"


def fetch_latest_version(timeout: float = 15) -> str:
    """Blocking: read the latest release's BeaconNet-version.txt (a few bytes, over HTTPS)."""
    try:
        import certifi  # bundled in the apps; macOS Python has no system CA store
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(f"{RELEASES}/download/BeaconNet-version.txt",
                                 headers={"User-Agent": f"BeaconNet-Desktop/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310 - fixed https URL
        v = r.read(64).decode("ascii", "replace").strip()
    if not VERSION_RE.match(v):
        raise ValueError("unexpected version file")
    return v


# --- installing an update (the built apps only) -------------------------------------------
# "Update now" downloads this computer's file from the latest release, checks it against
# BeaconNet-SHA256SUMS.txt from the same release, puts it where the running app is (the old
# one is kept as *.old until the next start), starts it on the same port and quits.

SUMS_FILE = "BeaconNet-SHA256SUMS.txt"
MAX_DOWNLOAD = 300 * 1024 * 1024


class UpdateError(Exception):
    """A reason the update can't be installed, worded for the person using the app."""


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def download_release_file(name: str, dest: Path | None = None, limit: int = MAX_DOWNLOAD) -> bytes | str:
    """Blocking: fetch one file of the latest release over HTTPS. With `dest`, stream it there
    and return its SHA-256; otherwise return the bytes (small files only)."""
    req = urllib.request.Request(f"{RELEASES}/download/{name}", headers={"User-Agent": f"BeaconNet-Desktop/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r:  # noqa: S310 - fixed https URL
        if dest is None:
            return r.read(limit)
        h, size = hashlib.sha256(), 0
        with open(dest, "wb") as f:
            while chunk := r.read(1 << 16):
                size += len(chunk)
                if size > limit:
                    raise UpdateError("The download is far bigger than expected, so it wasn't installed.")
                h.update(chunk)
                f.write(chunk)
        return h.hexdigest()


def install_target() -> Path | None:
    """What gets replaced: the .exe / Linux program, or the macOS .app. None when running
    from the Python source (the launchers update with git) or from somewhere read-only."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        app = next((p for p in exe.parents if p.suffix == ".app"), None)
        if app is None or "AppTranslocation" in str(app):     # run straight from Downloads
            return None
        return app
    return exe


def _rm(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists() or p.is_symlink():
        p.unlink()


def _launch(target: Path, port: int) -> None:
    args = ["--no-browser", "--port", str(port), "--after-update", str(os.getpid())]
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")   # a fresh copy, not this one's files
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-n", str(target), "--args", *args], env=env)
    elif sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(target), *args], env=env, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen([str(target), *args], env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install_update(target: Path, port: int, fetch=download_release_file, launch=_launch,
                   asset: str | None = None) -> None:
    """Blocking. Download, verify, swap and start the new app. On any failure the app in
    place is left (or put back) as it was and UpdateError explains what to do."""
    asset = asset or platform_asset()
    stage = target.parent / ".beaconnet-update"
    old = target.with_name(target.name + ".old")
    try:
        _rm(stage)
        stage.mkdir()
    except OSError:
        raise UpdateError(f"BeaconNet can't write to {target.parent}, so it can't update itself there. "
                          "Use Download update instead.") from None
    try:
        try:
            sums = {}
            for line in fetch(SUMS_FILE, None, 64 * 1024).decode("ascii", "replace").splitlines():
                parts = line.split()
                if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                    sums[parts[1].lstrip("*")] = parts[0]
            if asset not in sums:
                raise UpdateError("This release can't be installed from inside the app. Use Download update instead.")
            got = fetch(asset, stage / asset)
        except UpdateError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError("This release can't be installed from inside the app. Use Download update instead.") from None
            raise UpdateError(f"GitHub didn't send the update (HTTP {exc.code}). Try again later.") from None
        except Exception as exc:
            raise UpdateError(f"Couldn't download the update ({exc.__class__.__name__}). Is this computer online?") from None
        if got != sums[asset]:
            raise UpdateError("The download didn't match its checksum, so it wasn't installed. Try again later.")

        name = target.name
        if asset.endswith(".zip") and sys.platform == "darwin":
            out = stage / "x"
            subprocess.run(["ditto", "-x", "-k", str(stage / asset), str(out)], check=True, capture_output=True)
            new = out / "BeaconNet.app"
        elif asset.endswith(".zip"):
            new = stage / "new.exe"
            with zipfile.ZipFile(stage / asset) as z:
                new.write_bytes(z.read("BeaconNet.exe"))
        else:
            new = stage / "new"
            with tarfile.open(stage / asset) as t:
                m = t.getmember("BeaconNet")
                if not m.isreg():
                    raise UpdateError("The download isn't what was expected, so it wasn't installed.")
                new.write_bytes(t.extractfile(m).read())  # type: ignore[union-attr]
            new.chmod(0o755)
        if not new.exists():
            raise UpdateError("The download isn't what was expected, so it wasn't installed.")

        try:
            _rm(old)                  # left by the update before this one
        except OSError:
            raise UpdateError("An older update is still being tidied up. Try again in a minute.") from None
        target.rename(old)            # allowed while running, on Windows too
        try:
            new.rename(stage.parent / name)
            launch(target, port)
        except Exception:
            _rm(target) if target.exists() else None
            old.rename(target)
            raise
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"The update couldn't be installed ({exc.__class__.__name__}: {str(exc)[:120]}). "
                          "Use Download update instead.") from None
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def tidy_after_update() -> None:
    """Remove what the last update left behind (the old app may still be closing)."""
    target = install_target()
    if target is None:
        return
    for _ in range(12):
        try:
            _rm(target.with_name(target.name + ".old"))
            shutil.rmtree(target.parent / ".beaconnet-update", ignore_errors=True)
            return
        except OSError:
            time.sleep(5)


# --- settings ---------------------------------------------------------------------------


def resource(name: str) -> Path:
    """A file shipped with the app (next to this script, or inside a packaged .exe/.app)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name



def config_dir() -> Path:
    if os.environ.get("BEACONNET_HOME"):
        d = Path(os.environ["BEACONNET_HOME"])
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / "BeaconNet"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "BeaconNet"
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "beaconnet"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Settings:
    """host, port, username, pinned host key, last bulletin seen, notification choices."""

    DEFAULTS: dict[str, Any] = {"host": "", "port": 2222, "username": "", "host_key": None, "last_seen": None,
                                "notify_bulletins": True, "notify_emergency": True, "notify_mail": True,
                                "notify_registrations": True,
                                # Radio mode (Phase 3): saved now, used once nodes answer on packet radio.
                                "connection": "internet", "radio": {},
                                # App updates: check GitHub twice a day; "Not now" hides one version.
                                "check_updates": True, "skipped_version": None}

    def __init__(self, home: Path) -> None:
        self.home = home
        self.file = home / "settings.json"
        self.key_file = home / "device_key"
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(self.file.read_text()))
        except (OSError, ValueError):
            pass

    def __getitem__(self, k: str) -> Any:
        return self.data[k]

    def update(self, **kw: Any) -> None:
        self.data.update(kw)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.file)

    def has_key(self) -> bool:
        return self.key_file.exists()

    def forget(self) -> None:
        for f in (self.key_file, self.key_file.with_suffix(".pub")):
            try:
                f.unlink()
            except OSError:
                pass
        self.update(username="", host_key=None, last_seen=None)


# --- radio settings (Phase 3 pre-stage) ---------------------------------------------------

# AX.25 station: 1-6 letters/digits with at least one digit, optional -SSID 0-15.
AX25_CALL = re.compile(r"^(?=[A-Z0-9]*[0-9])[A-Z0-9]{1,6}(-(?:[0-9]|1[0-5]))?$")
RADIO_DEFAULTS: dict[str, Any] = {"my_call": "", "bbs_call": "", "via": [], "tnc": "kiss-tcp",
                                  "tnc_host": "127.0.0.1", "tnc_port": 8001, "serial_port": "", "serial_baud": 9600,
                                  "check_minutes": 30}


def clean_radio(d: dict) -> dict:
    """Validate radio settings; raises ValueError with a message the page shows."""
    r = dict(RADIO_DEFAULTS)
    call = lambda v: str(v or "").strip().upper()  # noqa: E731
    r["my_call"], r["bbs_call"] = call(d.get("my_call")), call(d.get("bbs_call"))
    for k, label in (("my_call", "Your callsign"), ("bbs_call", "BBS callsign")):
        if not AX25_CALL.match(r[k]):
            raise ValueError(f"{label} must look like M7XYZ or GM4ABC-10 (SSID 0-15)")
    if r["my_call"].split("-")[0] == r["bbs_call"].split("-")[0] and r["my_call"] == r["bbs_call"]:
        raise ValueError("your callsign and the BBS callsign must differ")
    via = d.get("via") or []
    if isinstance(via, str):
        via = [v for v in re.split(r"[\s,]+", via) if v]
    via = [call(v) for v in via]
    if len(via) > 2 or not all(AX25_CALL.match(v) for v in via):
        raise ValueError("Via: up to 2 digipeater callsigns, e.g. GM4XYZ-1")
    r["via"] = via
    r["tnc"] = d.get("tnc") if d.get("tnc") in ("kiss-tcp", "kiss-serial") else "kiss-tcp"
    if r["tnc"] == "kiss-tcp":
        host = str(d.get("tnc_host") or "127.0.0.1").strip()
        if not re.match(r"^[A-Za-z0-9.:_-]{1,255}$", host):
            raise ValueError("TNC address: a host name or IP, e.g. 127.0.0.1 for Dire Wolf on this computer")
        port = int(d.get("tnc_port") or 8001)
        if not 1 <= port <= 65535:
            raise ValueError("TNC port: 1-65535 (Dire Wolf's KISS port is 8001)")
        r["tnc_host"], r["tnc_port"] = host, port
    else:
        sp = str(d.get("serial_port") or "").strip()
        if not re.match(r"^(COM[0-9]{1,3}|/dev/[A-Za-z0-9._/-]{1,60})$", sp):
            raise ValueError("Serial port: e.g. COM3 (Windows) or /dev/ttyUSB0 (Linux, macOS)")
        baud = int(d.get("serial_baud") or 9600)
        if baud not in (1200, 4800, 9600, 19200, 38400, 57600, 115200):
            raise ValueError("Serial speed: one of 1200, 4800, 9600, 19200, 38400, 57600, 115200")
        r["serial_port"], r["serial_baud"] = sp, baud
    minutes = int(d.get("check_minutes") or 30)
    if not 10 <= minutes <= 240:
        raise ValueError("Check every: 10-240 minutes (the channel is shared, so don't call too often)")
    r["check_minutes"] = minutes
    return r


# --- desktop notifications ----------------------------------------------------------------

_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($env:BN_TITLE)) > $null
$t.Item(1).AppendChild($x.CreateTextNode($env:BN_BODY)) > $null
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def desktop_notify(title: str, body: str) -> None:
    """Best effort; the text is passed as data (env/argv), never as script."""
    title, body = title[:120], body[:240]
    try:
        if sys.platform == "win32":
            env = {**os.environ, "BN_TITLE": title, "BN_BODY": body}
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _PS_TOAST],
                             env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                              "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                              title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "--app-name=BeaconNet", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# --- connection to the BBS ----------------------------------------------------------------


class BBSError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class Link:
    """One SSH connection running the beaconnet-client subsystem."""

    def __init__(self, conn: asyncssh.SSHClientConnection, proc: asyncssh.SSHClientProcess) -> None:
        self.conn, self.proc = conn, proc
        self.next_id = 0
        self.lock = asyncio.Lock()

    @classmethod
    async def open(cls, host: str, port: int, username: str, password: str | None, key_file: Path | None,
                   host_key: str | None) -> tuple["Link", str]:
        seen: dict[str, str] = {}

        class Pin(asyncssh.SSHClient):
            def validate_host_public_key(self, h: str, addr: str, p: int, key: asyncssh.SSHKey) -> bool:
                k = key.export_public_key("openssh").decode().split()
                seen["key"] = " ".join(k[:2])
                return host_key is None or seen["key"] == host_key  # trust on first use, then pinned

        client_keys = [asyncssh.read_private_key(str(key_file))] if key_file and key_file.exists() else []
        try:
            conn, _ = await asyncio.wait_for(asyncssh.create_connection(
                Pin, host, port, username=username, client_keys=client_keys, password=password,
                known_hosts=([], [], []), agent_path=None, kbdint_auth=False, keepalive_interval=30,
                preferred_auth=("publickey", "password")), timeout=20)
        except asyncssh.HostKeyNotVerifiable as exc:
            raise BBSError("host_key_changed",
                           "The node's SSH key has CHANGED since you first connected. If the sysop didn't "
                           "rebuild it, someone may be intercepting the connection.") from exc
        except asyncssh.PermissionDenied as exc:
            raise BBSError("login_failed", "Login refused: check the username and password.") from exc
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
            raise BBSError("unreachable", f"Can't reach {host}:{port} ({exc.__class__.__name__}).") from exc
        try:
            proc = await conn.create_process(subsystem=SUBSYSTEM, encoding="utf-8")
        except asyncssh.Error as exc:
            conn.close()
            raise BBSError("no_client_api", "This node is too old for BeaconNet Desktop (needs v1.4.0+).") from exc
        return cls(conn, proc), seen.get("key", "")

    async def call(self, op: str, **args: Any) -> Any:
        async with self.lock:
            self.next_id += 1
            self.proc.stdin.write(json.dumps({"id": self.next_id, "op": op, **args}) + "\n")
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
        if not line:
            raise BBSError("disconnected", "The connection to the node closed.")
        r = json.loads(line)
        if not r.get("ok"):
            e = r.get("error") or {}
            raise BBSError(e.get("code", "error"), e.get("message", "error"))
        return r["data"]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


class App:
    def __init__(self, settings: Settings, notify=desktop_notify) -> None:  # type: ignore[no-untyped-def]
        self.s = settings
        self.notify = notify
        self.link: Link | None = None
        self.me: dict = {}
        self.info: dict = {}
        self.status = "not set up" if not settings["host"] else "connecting"
        self.error = ""
        self.events: list[dict] = []        # recent notifications, newest last (for the page)
        self.event_seq = 0
        self.notified_mail: set[int] = set()
        self.notified_pending: set[int] = set()
        self.password: str | None = None    # memory only, until a device key exists
        self.wake = asyncio.Event()
        self.checks = 0                     # first check after start lists mail/registrations quietly
        self.update: dict | None = None     # {"version", "download", "notes"} when a newer app exists
        self.update_error = ""
        self.update_checked = 0.0
        self.fetch_version = fetch_latest_version
        self.install_target = install_target
        self.install = install_update
        self.installing = False
        self.quit = lambda: os._exit(0)
        self._update_task: asyncio.Task | None = None

    # -- connect ----------------------------------------------------------------------

    async def connect(self) -> None:
        s = self.s
        key = s.key_file if s.has_key() else None
        if key is None and not self.password:
            self.status, self.error = "needs login", ""
            return
        link, host_key = await Link.open(s["host"], int(s["port"]), s["username"], self.password, key, s["host_key"])
        if s["host_key"] is None and host_key:
            s.update(host_key=host_key)
        hello = await link.call("hello")
        self.link, self.me, self.info = link, hello["me"], hello["info"]
        self.status, self.error = "connected", ""

    async def login(self, host: str, port: int, username: str, password: str, remember: bool) -> None:
        if self.link:
            self.link.close()
            self.link = None
        changed = (host, port, username) != (self.s["host"], self.s["port"], self.s["username"])
        if changed:
            self.s.forget()
            self.s.update(host=host, port=port, username=username.lower())
        self.password = password
        await self.connect()
        if remember and not self.s.has_key() and self.me.get("status") == "ACTIVE":
            await self.remember_device()
        self.wake.set()

    async def remember_device(self) -> None:
        """Make a key for this computer and add it to the account, so the password isn't needed again."""
        assert self.link
        key = asyncssh.generate_private_key("ssh-ed25519", comment=f"BeaconNet Desktop on {platform.node()[:30]}")
        pub = key.export_public_key("openssh").decode().strip()
        await self.link.call("add_key", public_key=pub)
        self.s.key_file.write_bytes(key.export_private_key("openssh"))
        self.s.key_file.with_suffix(".pub").write_text(pub + "\n")
        try:
            os.chmod(self.s.key_file, 0o600)
        except OSError:
            pass
        self.password = None

    def device_fingerprint(self) -> str | None:
        if not self.s.has_key():
            return None
        try:
            return asyncssh.read_private_key(str(self.s.key_file)).get_fingerprint("sha256")
        except (OSError, ValueError, asyncssh.KeyImportError):
            return None

    def logout(self) -> None:
        if self.link:
            self.link.close()
        self.link, self.me, self.password = None, {}, None
        self.s.forget()
        self.status = "needs login"

    # -- polling and notifications ------------------------------------------------------

    def _event(self, kind: str, title: str, body: str, ref: Any = None, pop: bool = True) -> None:
        self.event_seq += 1
        self.events = (self.events + [{"seq": self.event_seq, "kind": kind, "title": title, "body": body,
                                       "ref": ref, "at": time.strftime("%H:%M")}])[-100:]
        if pop:
            self.notify(title, body)

    async def check(self) -> None:
        assert self.link
        s = self.s
        first = self.checks == 0
        self.checks += 1
        f = await self.link.call("feed", after=s["last_seen"])
        for b in f["bulletins"]:
            if b.get("author") == s["username"] and b.get("origin_node") in (None, self.info.get("node_id")):
                continue  # your own post
            emergency = b.get("emergency_board") or b.get("priority") in ("EMERGENCY", "PRIORITY")
            who = b.get("callsign") or b.get("author") or "?"
            if emergency and s["notify_emergency"]:
                prio = b.get("priority") or "EMERGENCY"
                self._event("emergency", f"{prio}: {b['subject']}", f"{b.get('area') or ''} - {who} on {b['board']}".strip(" -"),
                            b["number"])
            elif s["notify_bulletins"]:
                self._event("bulletin", f"New on {b['board']}: {b['subject']}", f"from {who}", b["number"])
        latest = max([f["latest"], s["last_seen"] or 0] + [b["number"] for b in f["bulletins"]])
        if latest != s["last_seen"]:
            s.update(last_seen=latest)
        for m in f["unread_mail"]:
            if m["id"] not in self.notified_mail:
                self.notified_mail.add(m["id"])
                if s["notify_mail"]:
                    self._event("mail", f"Mail from {m['from']}", m["subject"], m["id"], pop=not first)
        for p in f.get("pending") or []:
            if p["id"] not in self.notified_pending:
                self.notified_pending.add(p["id"])
                if s["notify_registrations"]:
                    self._event("registration", "New registration waiting",
                                f"{p['username']}" + (f" ({p['callsign']})" if p.get("callsign") else ""), p["id"],
                                pop=not first)
        self.me["sysop"] = f.get("sysop", False)
        self.me["unread_mail"] = len(f["unread_mail"])
        if self.checks % INFO_REFRESH_CHECKS == 0:   # the node can be renamed while we stay connected
            self.info = (await self.link.call("hello"))["info"]

    # -- app updates ----------------------------------------------------------------------

    async def check_update(self) -> None:
        self.update_checked = time.time()
        try:
            latest = await asyncio.to_thread(self.fetch_version)
        except Exception as exc:  # no internet, GitHub down, proxy... try again later
            self.update_error = f"Couldn't check for updates ({exc.__class__.__name__}). Is this computer online?"
            return
        self.update_error = ""
        if version_tuple(latest) <= version_tuple(VERSION):
            self.update = None
            return
        new = self.update is None or self.update["version"] != latest
        self.update = {"version": latest, "download": f"{RELEASES}/download/{platform_asset()}", "notes": RELEASES,
                       "can_install": self.install_target() is not None}
        if new and latest != self.s["skipped_version"]:
            self._event("update", f"BeaconNet Desktop {latest} is available",
                        f"You have {VERSION}. Open BeaconNet to update.")

    def _maybe_check_update(self) -> None:
        due = time.time() - self.update_checked > UPDATE_CHECK_HOURS * 3600
        busy = self._update_task is not None and not self._update_task.done()
        if self.s["check_updates"] and due and not busy:
            self._update_task = asyncio.ensure_future(self.check_update())

    async def run(self) -> None:
        backoff = 5
        while True:
            self._maybe_check_update()
            try:
                if self.s["connection"] == "radio":
                    if self.link:
                        self.link.close()
                        self.link = None
                    self.status = "radio (Phase 3)"
                    self.error = ("Radio mode is set up and saved. It connects once BeaconNet nodes "
                                  "answer on packet radio (Phase 3).")
                elif self.link is None and self.s["host"]:
                    await self.connect()
                if self.link is not None:
                    await self.check()
                    backoff = 5
            except BBSError as exc:
                if self.link:
                    self.link.close()
                self.link = None
                self.status, self.error = ("needs login" if exc.code == "login_failed" else "offline"), exc.message
                if exc.code in ("login_failed", "host_key_changed"):
                    self.password = None
            except (asyncssh.Error, OSError, ValueError, asyncio.TimeoutError) as exc:
                if self.link:
                    self.link.close()
                self.link = None
                self.status, self.error = "offline", f"connection lost ({exc.__class__.__name__}); retrying"
            wait = POLL_SECONDS if self.link else min(backoff, 300)
            backoff = min(backoff * 2, 300)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def call(self, op: str, args: dict) -> Any:
        if self.link is None:
            raise BBSError("offline", self.error or "Not connected to the node.")
        try:
            return await self.link.call(op, **args)
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
            self.link.close()
            self.link = None
            self.wake.set()
            raise BBSError("offline", "Connection lost; reconnecting.") from exc

    def state(self, since: int) -> dict:
        s = self.s
        return {
            "version": VERSION, "status": self.status, "error": self.error,
            "host": s["host"], "port": s["port"], "username": s["username"], "remembered": s.has_key(),
            "me": self.me, "node": {k: self.info.get(k) for k in ("node_id", "node_name", "version", "disclaimer")},
            "sysop": bool(self.me.get("sysop")),
            "device_key": self.device_fingerprint(),
            "events": [e for e in self.events if e["seq"] > since],
            "notify": {k: s[k] for k in ("notify_bulletins", "notify_emergency", "notify_mail", "notify_registrations")},
            "connection": s["connection"], "radio": {**RADIO_DEFAULTS, **(s["radio"] or {})},
            "update": self.update and {**self.update, "skipped": self.update["version"] == s["skipped_version"],
                                       "installing": self.installing},
            "update_error": self.update_error, "check_updates": s["check_updates"],
            "update_checked": time.strftime("%d %b %H:%M", time.localtime(self.update_checked)) if self.update_checked else None,
        }


# --- the local web page -----------------------------------------------------------------

ALLOWED_OPS = {"boards", "messages", "message", "post", "mail", "mail_get", "mail_send", "directory",
               "me", "profile", "password", "keys", "revoke_key",
               "admin_users", "admin_user", "admin_status"}


class Web:
    """A tiny HTTP server on 127.0.0.1 only. Every API call must carry this run's random
    token, and the Host header must be local, so other websites can't drive it."""

    def __init__(self, app: App, port: int) -> None:
        self.app, self.port = app, port
        self.token = secrets.token_urlsafe(24)
        self.page = resource("ui.html").read_text(encoding="utf-8")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            hdr = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
            n = int(hdr.get("content-length", "0") or 0)
            if n > 200_000:
                return await self.reply(writer, 413, {"error": "too large"})
            body = await reader.readexactly(n) if n else b""
            if hdr.get("host") not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
                return await self.reply(writer, 403, {"error": "bad host"})
            if method == "GET" and path in ("/", "/index.html"):
                html = self.page.replace("__TOKEN__", self.token)
                return await self.reply(writer, 200, html.encode(), "text/html; charset=utf-8")
            if hdr.get("x-beaconnet-token") != self.token:
                return await self.reply(writer, 403, {"error": "bad token"})
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError("object expected")
            return await self.reply(writer, 200, await self.api(method, path.split("?")[0], data))
        except BBSError as exc:
            await self.reply(writer, 200, {"error": exc.message, "code": exc.code})
        except (ValueError, KeyError, TypeError) as exc:
            await self.reply(writer, 400, {"error": str(exc)[:200]})
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def api(self, method: str, path: str, d: dict) -> Any:
        app = self.app
        if path == "/api/state":
            return app.state(int(d.get("since", 0)))
        if path == "/api/login":
            host = str(d["host"]).strip()
            if not host or any(ch.isspace() for ch in host) or len(host) > 255:
                raise ValueError("enter the node's address")
            await app.login(host, int(d.get("port") or 2222), str(d["username"]).strip(), str(d["password"]),
                            bool(d.get("remember", True)))
            return app.state(0)
        if path == "/api/logout":
            app.logout()
            return {"ok": True}
        if path == "/api/connection":
            mode = d.get("connection")
            if mode not in ("internet", "radio"):
                raise ValueError("connection must be internet or radio")
            if mode == "radio":
                app.s.update(radio=clean_radio(d.get("radio") or {}), connection="radio")
            else:
                app.s.update(connection="internet")
                app.status = "connecting" if app.s["host"] else "not set up"
                app.error = ""
            app.wake.set()
            return app.state(0)
        if path == "/api/notify":
            app.s.update(**{k: bool(v) for k, v in d.items() if k in Settings.DEFAULTS and k.startswith("notify_")})
            return {"ok": True}
        if path == "/api/update/check":            # asking by hand also un-hides a skipped version
            app.s.update(skipped_version=None)
            await app.check_update()
            return app.state(0)
        if path == "/api/update/auto":
            app.s.update(check_updates=bool(d.get("on")))
            return app.state(0)
        if path == "/api/update/skip":
            if app.update:
                app.s.update(skipped_version=app.update["version"])
            return app.state(0)
        if path == "/api/update/install":
            target = app.install_target()
            if not app.update or target is None or app.installing:
                raise ValueError("there is no update to install here")
            app.installing = True
            try:
                await asyncio.to_thread(app.install, target, self.port)
            except UpdateError as exc:
                raise ValueError(str(exc)) from None
            finally:
                app.installing = False
            # The new copy is starting and waits for this one to let go of the port.
            asyncio.get_running_loop().call_later(1.0, app.quit)
            return {"ok": True, "restarting": True}
        if path == "/api/update/open":
            # Only ever opens our own release links, never a URL from the page.
            if app.update and d.get("what") in ("download", "notes"):
                webbrowser.open(app.update[d["what"]])
            return {"ok": True}
        if path == "/api/refresh":
            app.wake.set()
            return {"ok": True}
        if path == "/api/call":
            op = d.pop("op", None)
            if op not in ALLOWED_OPS:
                raise ValueError("unknown operation")
            result = await app.call(op, d)
            if op == "admin_status":
                app.wake.set()
            if op in ("profile", "password", "me"):
                app.me.update({k: v for k, v in (await app.call("me", {})).items() if k != "sysop"})
            return result
        raise ValueError("unknown path")

    async def reply(self, w: asyncio.StreamWriter, code: int, body: Any, ctype: str = "application/json") -> None:
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 413: "Too Large"}.get(code, "OK")
        w.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\nX-Frame-Options: DENY\r\nReferrer-Policy: no-referrer\r\n"
                "Content-Security-Policy: default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'\r\n"
                "Connection: close\r\n\r\n".encode() + body)
        try:
            await w.drain()
        except ConnectionError:
            pass


def open_window(url: str) -> None:
    """Open the page in its own app-style window (Edge/Chrome/Chromium --app), or else in
    the default browser."""
    candidates: list[list[str]] = []
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if root:
                candidates += [[str(Path(root) / "Microsoft/Edge/Application/msedge.exe")],
                               [str(Path(root) / "Google/Chrome/Application/chrome.exe")]]
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            candidates.append([f"/Applications/{app}.app/Contents/MacOS/{app}"])
    else:
        for exe in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge", "brave-browser"):
            if shutil.which(exe):
                candidates.append([shutil.which(exe)])  # type: ignore[list-item]
    for cmd in candidates:
        if Path(cmd[0]).exists():
            try:
                subprocess.Popen(cmd + [f"--app={url}", "--window-size=1000,760"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=sys.platform != "win32")
                return
            except OSError:
                continue
    webbrowser.open(url)


def tray_icon_image():  # type: ignore[no-untyped-def]
    """A beacon: an orange flame on a dark tower, drawn so no image file is needed."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(27, 31, 36, 255))
    d.polygon([(24, 56), (40, 56), (36, 30), (28, 30)], fill=(220, 224, 230, 255))
    d.polygon([(32, 8), (42, 26), (32, 32), (22, 26)], fill=(226, 115, 63, 255))
    d.ellipse((28, 16, 36, 26), fill=(255, 205, 90, 255))
    return img


def run_with_tray(url: str, background) -> bool:  # type: ignore[no-untyped-def]
    """Show a tray / menu-bar icon (Open, Quit) and run `background` in a thread.
    Returns False if no tray is possible here, so the caller runs without one."""
    try:
        import pystray
    except Exception:
        return False
    def quit_app(icon, _item=None):  # type: ignore[no-untyped-def]
        icon.stop()
        os._exit(0)

    try:
        icon = pystray.Icon("BeaconNet", tray_icon_image(), "BeaconNet", menu=pystray.Menu(
            pystray.MenuItem("Open BeaconNet", lambda i, _=None: open_window(url), default=True),
            pystray.MenuItem("Quit", quit_app)))
    except Exception:
        return False
    worker = threading.Thread(target=background, daemon=True)
    worker.start()
    try:
        icon.run()  # must own the main thread on macOS
    except Exception:
        worker.join()  # no tray after all (e.g. no desktop session): keep running without one
    return True


async def serve(args: argparse.Namespace, started) -> None:  # type: ignore[no-untyped-def]
    app = App(Settings(config_dir()))
    web = Web(app, args.port)
    server = await asyncio.start_server(web.handle, "127.0.0.1", args.port)
    started()
    async with server:
        await app.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="BeaconNet Desktop - notifications for your BBS node")
    ap.add_argument("--port", type=int, default=8765, help="local port for the page (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the window on start")
    ap.add_argument("--no-tray", action="store_true", help="no tray / menu-bar icon (e.g. on a server)")
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("--after-update", type=int, metavar="PID", help=argparse.SUPPRESS)
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}/"

    import socket
    if args.after_update:
        # Started by "Update now": give the old copy up to 30 seconds to free the port.
        for _ in range(60):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", args.port)) != 0:
                    break
            time.sleep(0.5)
        threading.Thread(target=tidy_after_update, daemon=True).start()
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            # Already running: just show it.
            if not args.no_browser:
                open_window(url)
            print(f"BeaconNet Desktop is already running: {url}")
            return

    def started() -> None:
        print(f"BeaconNet Desktop {VERSION} - {url}  (Ctrl+C to stop)")
        if not args.no_browser:
            open_window(url)

    def background() -> None:
        try:
            asyncio.run(serve(args, started))
        except OSError as exc:
            desktop_notify("BeaconNet can't start", f"Port {args.port} is in use ({exc.strerror}).")
            os._exit(1)

    if not args.no_tray and run_with_tray(url, background):
        return
    try:
        background()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
