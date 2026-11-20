"""The beaconnet-client SSH subsystem and the BeaconNet Desktop app, over real SSH."""

import asyncio
import importlib.util
import json
from pathlib import Path

import asyncssh
import pytest

from conftest import ROOT, gen_key, pub

pytestmark = pytest.mark.asyncio


def load_desktop():
    spec = importlib.util.spec_from_file_location("beaconnet_desktop", ROOT / "clients/desktop/beaconnet_desktop.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


async def client(port: int, username: str, key=None, password=None):
    conn = await asyncssh.connect("127.0.0.1", port, username=username, client_keys=[key] if key else [],
                                  password=password, known_hosts=None, agent_path=None, kbdint_auth=False)
    proc = await conn.create_process(subsystem="beaconnet-client", encoding="utf-8")
    n = 0

    async def call(op, **kw):
        nonlocal n
        n += 1
        proc.stdin.write(json.dumps({"id": n, "op": op, **kw}) + "\n")
        r = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
        assert r["id"] == n
        return r
    return conn, call


def lan(bbs, nets=("127.0.0.0/8",)):
    from bbs_core.config import SysopCfg
    bbs.cfg = bbs.cfg.model_copy(update={"sysop": SysopCfg(allowed_networks=list(nets))})


async def test_client_api_basics_and_feed(gateways, bbs, admin_key):
    ssh_port, _, _ = gateways
    conn, call = await client(ssh_port, "sysop1", admin_key)
    hello = (await call("hello"))["data"]
    assert hello["protocol"] == 1 and hello["me"]["username"] == "sysop1" and hello["info"]["node_id"]
    first = (await call("feed"))["data"]
    assert first["bulletins"] == [] and first["sysop"] is False and "pending" not in first  # not LAN
    r = await call("post", board="EMERGENCY", subject="Bridge closed", body="A82 bridge shut", priority="EMERGENCY",
                   area="Glen", expires_hours=24)
    assert r["ok"], r
    feed = (await call("feed", after=first["latest"]))["data"]
    assert [b["subject"] for b in feed["bulletins"]] == ["Bridge closed"]
    assert feed["bulletins"][0]["emergency_board"] is True
    msg = (await call("message", number=feed["latest"]))["data"]
    assert msg["body"] == "A82 bridge shut"
    # Unknown ops, bad types and admin ops from the internet are refused.
    assert (await call("shell"))["error"]["code"] == "bad_request"
    assert (await call("message", number="1; DROP"))["error"]["code"] == "bad_request"
    assert (await call("admin_users"))["error"]["code"] == "forbidden"
    conn.close()


async def test_client_api_sysop_only_from_lan(gateways, bbs, admin_key, ssh_api):
    ssh_port, _, _ = gateways
    lan(bbs)
    from conftest import register
    register(ssh_api, "newbie", gen_key())
    conn, call = await client(ssh_port, "sysop1", admin_key)
    feed = (await call("feed"))["data"]
    assert feed["sysop"] is True and [p["username"] for p in feed["pending"]] == ["newbie"]
    users = (await call("admin_users", status="PENDING"))["data"]["users"]
    r = await call("admin_status", user_id=users[0]["id"], action="approve")
    assert r["ok"] and r["data"]["status"] == "ACTIVE"
    assert (await call("feed"))["data"]["pending"] == []
    conn.close()


async def test_ordinary_user_gets_no_admin_even_on_lan(gateways, bbs, active_user):
    ssh_port, _, _ = gateways
    lan(bbs)
    name, key, _ = active_user
    conn, call = await client(ssh_port, name, key)
    feed = (await call("feed"))["data"]
    assert feed["sysop"] is False and "pending" not in feed
    assert (await call("admin_users"))["error"]["code"] == "forbidden"
    assert (await call("admin_status", user_id=1, action="approve"))["error"]["code"] == "forbidden"
    conn.close()


async def test_guest_and_register_logins_cannot_use_client_api(gateways):
    ssh_port, _, _ = gateways
    for user in ("guest", "new"):
        conn = await asyncssh.connect("127.0.0.1", ssh_port, username=user, client_keys=[], password="x",
                                      known_hosts=None, agent_path=None)
        proc = await conn.create_process(subsystem="beaconnet-client", encoding="utf-8")
        out = await asyncio.wait_for(proc.stdout.read(), 10)
        assert "menu only" in out
        conn.close()


async def test_admin_console_has_no_client_api(gateways, admin_key):
    _, admin_port, _ = gateways
    conn = await asyncssh.connect("127.0.0.1", admin_port, username="sysop1", client_keys=[admin_key],
                                  known_hosts=None, agent_path=None)
    proc = await conn.create_process(subsystem="beaconnet-client", encoding="utf-8")
    assert "menu only" in await asyncio.wait_for(proc.stdout.read(), 10)
    conn.close()


async def test_desktop_app_end_to_end(gateways, bbs, admin_key, ssh_api, tmp_path, monkeypatch):
    """Password login, 'remember this computer', notifications, pinned host key."""
    ssh_port, _, _ = gateways
    desk = load_desktop()
    lan(bbs)
    # A user who registered with a password (no key) and was approved.
    from bbs_core.security import hash_password
    bbs.bootstrap_admin("deskop", None, None, pub(gen_key()))
    with bbs.db.session() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = 'deskop'", (hash_password("beacon fire 99"),))

    popped = []
    settings = desk.Settings(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    settings = desk.Settings(tmp_path / "home")
    app = desk.App(settings, notify=lambda t, b: popped.append((t, b)))

    with pytest.raises(desk.BBSError) as bad:
        await app.login("127.0.0.1", ssh_port, "deskop", "wrong password", True)
    assert bad.value.code == "login_failed"

    await app.login("127.0.0.1", ssh_port, "deskop", "beacon fire 99", True)
    assert app.status == "connected" and settings.has_key() and app.password is None
    assert settings["host_key"].startswith("ssh-ed25519 ")
    await app.check()                      # first check: remembers where we are, no pop-ups
    assert popped == []

    # News arrives: a bulletin, an emergency, mail and a registration.
    alice_key = gen_key()
    from conftest import approve, register
    register(ssh_api, "alice", alice_key)
    admin_api_session = ssh_api.login("sysop1", admin_key, ip="127.0.0.1").json()["token"]
    users = ssh_api.call("GET", "/v1/admin/users", admin_api_session, params={"status": "PENDING"}).json()["users"]
    ssh_api.call("POST", f"/v1/admin/users/{users[0]['id']}/status", admin_api_session, json={"action": "approve"})
    s_alice = ssh_api.login("alice", alice_key, ip="203.0.113.5").json()["token"]
    ssh_api.call("POST", "/v1/messages", s_alice, json={"board": "GENERAL", "subject": "Net tonight", "body": "19:00"})
    ssh_api.call("POST", "/v1/messages", s_alice, json={"board": "EMERGENCY", "subject": "Power cut", "body": "x",
                                                       "priority": "EMERGENCY", "area": "Village"})
    ssh_api.call("POST", "/v1/mail", s_alice, json={"to": "deskop", "subject": "Hello", "body": "hi"})
    register(ssh_api, "waiting1", gen_key(), ip="203.0.113.7")
    await app.check()
    titles = [t for t, _ in popped]
    assert "New on GENERAL: Net tonight" in titles
    assert "EMERGENCY: Power cut" in titles
    assert "Mail from alice" in titles and "New registration waiting" in titles
    n = len(popped)
    await app.check()
    assert len(popped) == n                # nothing twice

    # Next start: logs in with the remembered key, no password.
    app.link.close()
    app2 = desk.App(desk.Settings(tmp_path / "home"), notify=lambda t, b: None)
    await app2.connect()
    assert app2.status == "connected" and app2.me["username"] == "deskop"
    app2.link.close()

    # A different host key (node rebuilt, or someone in the middle) is refused.
    settings2 = desk.Settings(tmp_path / "home")
    settings2.update(host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl")
    app3 = desk.App(settings2, notify=lambda t, b: None)
    with pytest.raises(desk.BBSError) as changed:
        await app3.connect()
    assert changed.value.code == "host_key_changed"


async def test_desktop_web_page_guards(gateways, tmp_path):
    desk = load_desktop()
    app = desk.App(desk.Settings(tmp_path), notify=lambda t, b: None)
    web = desk.Web(app, 8765)

    async def req(raw: bytes) -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        out = bytearray()

        class W:
            def write(self, b): out.extend(b)
            async def drain(self): pass
            def close(self): pass
        await web.handle(reader, W())
        return bytes(out)

    page = await req(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n")
    assert b"200 OK" in page and web.token.encode() in page and b"__TOKEN__" not in page
    body = b'{"since":0}'
    no_token = await req(b"POST /api/state HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nContent-Length: 11\r\n\r\n" + body)
    assert b"403" in no_token.split(b"\r\n")[0]
    rebinding = await req(b"POST /api/state HTTP/1.1\r\nHost: evil.example:8765\r\nX-BeaconNet-Token: " +
                          web.token.encode() + b"\r\nContent-Length: 11\r\n\r\n" + body)
    assert b"403" in rebinding.split(b"\r\n")[0]
    ok = await req(b"POST /api/state HTTP/1.1\r\nHost: localhost:8765\r\nX-BeaconNet-Token: " +
                   web.token.encode() + b"\r\nContent-Length: 11\r\n\r\n" + body)
    assert b"200 OK" in ok and b'"status"' in ok
    bad_op = json.dumps({"op": "add_key", "public_key": "x"}).encode()
    r = await req(b"POST /api/call HTTP/1.1\r\nHost: localhost:8765\r\nX-BeaconNet-Token: " + web.token.encode() +
                  f"\r\nContent-Length: {len(bad_op)}\r\n\r\n".encode() + bad_op)
    assert b"unknown operation" in r


async def test_users_edit_their_own_details(gateways, bbs, active_user, ssh_api, admin_key):
    ssh_port, _, _ = gateways
    name, key, _ = active_user                       # alice, callsign GM4AAA (verified on approval? no: claim)
    conn, call = await client(ssh_port, name, key)
    me = (await call("me"))["data"]
    assert me["callsign"] == "GM4AAA" and "contact" in me
    r = await call("profile", display_name="Alice B", contact="07700 900123", callsign="gm4zzz")
    assert r["ok"] and r["data"]["changed"] == ["callsign", "contact", "display_name"]
    me = (await call("me"))["data"]
    assert (me["display_name"], me["contact"], me["callsign"], me["callsign_verified"]) == \
        ("Alice B", "07700 900123", "GM4ZZZ", False)
    # Can't take someone else's callsign (the sysop's), or a username as a callsign.
    assert (await call("profile", callsign="GM0SYS"))["error"]["code"] == "conflict"
    assert (await call("profile", callsign="not a call"))["error"]["code"] == "invalid_callsign"
    # Clearing works; unknown fields are refused by the gateway, not passed on.
    assert (await call("profile", callsign=None))["ok"]
    assert (await call("me"))["data"]["callsign"] is None
    assert (await call("profile", role="admin"))["data"]["changed"] == []
    # Password: set, then usable for login.
    assert (await call("password", password="short"))["error"]["code"] == "invalid_password"
    assert (await call("password", password="a good long one"))["ok"]
    keys = (await call("keys"))["data"]["keys"]
    assert len(keys) == 1
    conn.close()
    c2, call2 = await client(ssh_port, name, password="a good long one")
    assert (await call2("me"))["data"]["has_password"] is True
    # With a password set, the last key can be removed.
    assert (await call2("revoke_key", key_id=keys[0]["id"]))["ok"]
    c2.close()
    with pytest.raises(asyncssh.PermissionDenied):
        await client(ssh_port, name, key)
    with bbs.db.session() as conn_db:
        acts = [r[0] for r in conn_db.execute("SELECT action FROM audit_log WHERE action IN ('profile_updated','password_set','key_revoked')")]
    assert acts.count("profile_updated") == 2 and "password_set" in acts and "key_revoked" in acts


async def test_username_callsign_must_match_and_pending_can_edit(gateways, bbs, ssh_api, admin_key):
    ssh_port, _, _ = gateways
    from conftest import register
    k = gen_key()
    register(ssh_api, "gm3abc", k, callsign="GM3ABC")  # username is a callsign: must stay
    conn, call = await client(ssh_port, "gm3abc", k)
    assert (await call("profile", callsign="GM3XYZ"))["error"]["code"] == "invalid_callsign"
    assert (await call("profile", display_name="Pending person"))["ok"]   # PENDING users may edit details
    assert (await call("password", password="long enough pw"))["error"]["code"] == "forbidden"  # not yet approved
    conn.close()


async def test_member_directory_for_mail(gateways, bbs, active_user, ssh_api, admin_key):
    ssh_port, _, _ = gateways
    from conftest import register
    name, key, _ = active_user
    pk = gen_key()
    register(ssh_api, "pendingpat", pk)
    conn, call = await client(ssh_port, name, key)
    assert (await call("profile", contact="07700 900123"))["ok"]
    d = (await call("directory"))["data"]
    by = {u["username"]: u for u in d["users"]}
    assert d["node"] and set(by) == {"sysop1", "alice"}          # pending accounts aren't listed
    assert by["alice"]["you"] is True and by["sysop1"]["sysop"] is True
    assert by["alice"]["callsign"] is None                         # claimed, not verified: not shown
    assert all(set(u) == {"username", "display_name", "callsign", "sysop", "you"} for u in d["users"])
    assert "07700" not in json.dumps(d)                            # contact details never listed
    assert (await call("mail_send", to="sysop1", subject="hi", body="from the list"))["ok"]
    conn.close()
    c2, call2 = await client(ssh_port, "pendingpat", pk)
    assert (await call2("directory"))["error"]["code"] == "forbidden"
    c2.close()


def test_desktop_radio_settings_saved_and_checked(tmp_path):
    desk = load_desktop()
    ok = desk.clean_radio({"my_call": "m7xyz", "bbs_call": "GM4ABC-10", "via": "gm4xyz-1, MB7ABC",
                           "tnc": "kiss-tcp", "tnc_port": 8001, "check_minutes": 30})
    assert ok["my_call"] == "M7XYZ" and ok["via"] == ["GM4XYZ-1", "MB7ABC"] and ok["tnc_host"] == "127.0.0.1"
    ser = desk.clean_radio({"my_call": "M7XYZ", "bbs_call": "GM4ABC-10", "tnc": "kiss-serial",
                            "serial_port": "COM3", "serial_baud": 9600})
    assert ser["serial_port"] == "COM3"
    for bad in ({"my_call": "", "bbs_call": "GM4ABC-10"}, {"my_call": "M7XYZ", "bbs_call": "GM4ABC-99"},
                {"my_call": "M7XYZ", "bbs_call": "M7XYZ"}, {"my_call": "M7XYZ", "bbs_call": "GM4ABC", "via": "A1 B2 C3"},
                {"my_call": "M7XYZ", "bbs_call": "GM4ABC", "tnc": "kiss-serial", "serial_port": "rm -rf"},
                {"my_call": "M7XYZ", "bbs_call": "GM4ABC", "check_minutes": 1}):
        with pytest.raises(ValueError):
            desk.clean_radio(bad)
    s = desk.Settings(tmp_path)
    s.update(connection="radio", radio=ok)
    assert desk.Settings(tmp_path)["radio"]["bbs_call"] == "GM4ABC-10"


async def test_desktop_picks_up_node_rename_while_connected(gateways, bbs, admin_key, tmp_path):
    ssh_port, _, _ = gateways
    desk = load_desktop()
    from bbs_core.security import hash_password
    bbs.bootstrap_admin("renamer", None, None, pub(gen_key()))
    with bbs.db.session() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = 'renamer'", (hash_password("beacon fire 99"),))
    (tmp_path / "home").mkdir()
    app = desk.App(desk.Settings(tmp_path / "home"), notify=lambda t, b: None)
    await app.login("127.0.0.1", ssh_port, "renamer", "beacon fire 99", False)
    old = app.info["node_name"]
    bbs.cfg = bbs.cfg.model_copy(update={"node": bbs.cfg.node.model_copy(update={"name": "Oban Packet Radio BBS"})})
    for _ in range(desk.INFO_REFRESH_CHECKS):
        await app.check()
    assert old != "Oban Packet Radio BBS" and app.info["node_name"] == "Oban Packet Radio BBS"
    app.link.close()


async def test_desktop_update_check(tmp_path, monkeypatch):
    desk = load_desktop()
    popped, opened = [], []
    app = desk.App(desk.Settings(tmp_path), notify=lambda t, b: popped.append(t))
    web = desk.Web(app, 8765)
    monkeypatch.setattr(desk.webbrowser, "open", opened.append)
    major, minor, patch = desk.version_tuple(desk.VERSION)

    app.fetch_version = lambda: desk.VERSION                        # up to date
    await app.check_update()
    assert app.update is None and popped == [] and app.state(0)["update"] is None

    newer = f"{major}.{minor + 1}.0"
    app.fetch_version = lambda: newer
    await app.check_update()
    st = app.state(0)
    assert st["update"]["version"] == newer and st["update"]["skipped"] is False
    assert st["update"]["download"].startswith(desk.RELEASES + "/download/BeaconNet-")
    assert popped == [f"BeaconNet Desktop {newer} is available"]
    await app.check_update()                                         # same news: no second pop-up
    assert len(popped) == 1

    await web.api("POST", "/api/update/open", {"what": "download"})
    await web.api("POST", "/api/update/open", {"what": "https://evil.example/"})   # never a page-supplied URL
    assert opened == [st["update"]["download"]]
    await web.api("POST", "/api/update/skip", {})                     # "Not now" hides this version
    assert app.state(0)["update"]["skipped"] is True
    st = await web.api("POST", "/api/update/check", {})               # checking by hand shows it again
    assert st["update"]["skipped"] is False

    def offline():
        raise OSError("no route")
    app.fetch_version = offline
    await app.check_update()
    assert "Couldn't check" in app.state(0)["update_error"] and app.update is not None
    await web.api("POST", "/api/update/auto", {"on": False})
    assert app.s["check_updates"] is False
    app.update_checked = 0
    app._maybe_check_update()
    assert app._update_task is None                                  # automatic checks really off



async def test_directory_search_and_paging_for_big_nodes(gateways, bbs, active_user):
    ssh_port, _, _ = gateways
    name, key, _ = active_user
    with bbs.db.session() as conn:
        for i in range(120):
            conn.execute("INSERT INTO users(username, display_name, role, status, callsign, callsign_verified, created_at,"
                         " updated_at) VALUES (?, ?, 'user', 'ACTIVE', ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                         (f"member{i:03d}", f"Member {i}", f"GM{i % 10}X{i:03d}"[:6], i % 2))
        conn.execute("INSERT INTO users(username, display_name, role, status, created_at, updated_at)"
                     " VALUES ('pct_user', '100% sure', 'user', 'ACTIVE', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    conn_, call = await client(ssh_port, name, key)
    d = (await call("directory", limit=50))["data"]
    assert d["total"] == 123 and len(d["users"]) == 50 and d["more"] is True
    d2 = (await call("directory", limit=50, offset=100))["data"]
    assert len(d2["users"]) == 23 and d2["more"] is False
    assert not {u["username"] for u in d["users"]} & {u["username"] for u in d2["users"]}
    hit = (await call("directory", q="Member 11", limit=50))["data"]          # display name
    assert {u["username"] for u in hit["users"]} == {f"member{i:03d}" for i in (11, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119)}
    assert (await call("directory", q="ALI"))["data"]["users"][0]["username"] == "alice"   # username, any case
    odd = (await call("directory", q="gm1x"))["data"]                         # only verified callsigns are searchable
    assert odd["total"] >= 1 and all(u["callsign"] for u in odd["users"])
    assert [u["username"] for u in (await call("directory", q="%"))["data"]["users"]] == ["pct_user"]   # no wildcards
    assert (await call("directory", q="_"))["data"]["users"][0]["username"] == "pct_user"
    assert (await call("directory", limit=0))["error"]["code"] == "bad_request"
    conn_.close()
