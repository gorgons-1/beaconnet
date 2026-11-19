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
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

try:
    import asyncssh
except ImportError:  # pragma: no cover - the launchers install it
    sys.exit("BeaconNet Desktop needs the 'asyncssh' package:  python -m pip install --user asyncssh")

VERSION = "1.1.0"
SUBSYSTEM = "beaconnet-client"
POLL_SECONDS = 30


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
                                "notify_registrations": True}

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

    async def run(self) -> None:
        backoff = 5
        while True:
            try:
                if self.link is None and self.s["host"]:
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
        }


# --- the local web page -----------------------------------------------------------------

ALLOWED_OPS = {"boards", "messages", "message", "post", "mail", "mail_get", "mail_send",
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
        if path == "/api/notify":
            app.s.update(**{k: bool(v) for k, v in d.items() if k in Settings.DEFAULTS and k.startswith("notify_")})
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
    import threading

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
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}/"

    import socket
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
