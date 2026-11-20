"""End to end over real SSH: guest, registration, admin approval, posting, mail, no shell."""

import asyncio

import asyncssh
import pytest

from conftest import connect, gen_key, pub

pytestmark = pytest.mark.asyncio


async def test_guest_menu_and_reading(gateways, bbs, admin_key):
    ssh_port, _, _ = gateways
    t = await connect(ssh_port, "guest")
    screen = await t.expect("Selection:")
    assert "BEACONNET" in screen and "Node: BEACON-HUB-01" in screen
    assert "3. Register New Account" in screen and "Connection: SSH" in screen
    t.send("2")
    screen = await t.expect("(Q=back):")
    assert "EMERGENCY" in screen and "ANNOUNCEMENTS" in screen
    t.send("GENERAL")
    await t.expect("Q=back:")
    t.send("Q")
    await t.expect("(Q=back):")
    t.send("Q")
    await t.expect("Selection:")
    t.send("5")
    screen = await t.expect("-- press Enter --")
    assert "local emergency number" in screen
    t.send("")
    await t.expect("Selection:")
    t.send("Q")
    await t.expect("73")
    await t.close()


async def test_full_registration_approval_and_posting(gateways, bbs):
    ssh_port, admin_port, _ = gateways
    user_key = gen_key()

    # 1. Register through the new@ login. The BBS picks up the key the client offers
    #    and asks to use it - nothing to copy and paste.
    t = await connect(ssh_port, "new", user_key)
    await t.expect("Username")
    t.send("hamish")
    await t.expect("callsign")
    t.send("gm7ham")
    await t.expect("Display name")
    t.send("Hamish")
    await t.expect("Contact")
    t.send("")
    screen = await t.expect("Use this device's key to log in?")
    assert user_key.get_fingerprint("sha256") in screen
    t.send("y")
    screen = await t.expect("Submit this registration?")
    assert "this device's key (automatic)" in screen
    t.send("y")
    screen = await t.expect("-- press Enter --")
    assert "PENDING" in screen and "log you in by itself" in screen
    t.send("")
    await t.close()

    # 2. Pending user logs in with the key: read-only menu.
    t = await connect(ssh_port, "hamish", user_key)
    screen = await t.expect("Selection:")
    assert "Account: PENDING" in screen and "Post a Bulletin" not in screen
    await t.close()

    # 3. An administrator (created by the bootstrap path) approves it in the admin console.
    admin_key = gen_key()
    bbs.bootstrap_admin("sysop9", None, None, pub(admin_key))

    a = await connect(admin_port, "sysop9", admin_key)
    screen = await a.expect("Selection:")
    assert "BEACONNET - ADMINISTRATION" in screen and "1. Pending Registrations (1)" in screen
    a.send("1")
    screen = await a.expect("Q=back:")
    assert "hamish" in screen
    uid = [ln for ln in screen.splitlines() if "hamish" in ln][0].split(".")[0].strip()
    a.send(uid)
    screen = await a.expect("Q=back:")
    assert "UNVERIFIED claim" in screen and "GM7HAM" in screen
    a.send("A")
    await a.expect("belongs to this person?")
    a.send("y")
    screen = await a.expect("Q=back:")
    assert "Approved" in screen
    a.send("Q")
    await a.expect("Q=back:")
    a.send("Q")
    await a.expect("Selection:")
    a.send("8")
    screen = await a.expect("Q=back:")
    assert "user_approve" in screen and "success" in screen
    a.send("Q")
    await a.expect("Selection:")
    a.send("Q")
    await a.close()

    # 4. Approved user posts a bulletin and sends mail.
    t = await connect(ssh_port, "hamish", user_key, pty=True)
    screen = await t.expect("Selection:")
    assert "3. Post a Bulletin" in screen
    t.send("3")
    await t.expect("Board:")
    t.send("radio")
    await t.expect("Subject:")
    t.send("Test from Skye")
    await t.expect("/EX")
    t.send("Line one of the message.")
    t.send("Line two.")
    t.send("/EX")
    await t.expect("Send this bulletin?")
    t.send("y")
    screen = await t.expect("Selection:")
    assert "Posted as message #" in screen
    number = screen.split("Posted as message #")[1].split()[0]
    t.send(f"R {number}")
    screen = await t.expect("Enter=back:")
    assert "Line one of the message." in screen and "GM7HAM" in screen
    t.send("")
    await t.expect("Selection:")
    t.send("5")
    await t.expect("Q=back:")
    t.send("C")
    await t.expect("To (username")
    t.send("sysop9")
    await t.expect("Subject:")
    t.send("Thanks")
    await t.expect("/EX")
    t.send("Thanks for approving me.")
    t.send(".")
    screen = await t.expect("Q=back:")
    assert "DELIVERED" in screen
    t.send("Q")
    await t.expect("Selection:")
    t.send("Q")
    await t.expect("73")
    # On a pty, output lines end CRLF and nothing doubles the CR.
    assert "\r\n" in t.all and "\r\r\n" not in t.all
    await t.close()


async def test_no_shell_exec_or_subsystems(gateways):
    ssh_port, _, _ = gateways
    conn = await asyncssh.connect("127.0.0.1", ssh_port, username="guest", client_keys=[], known_hosts=None, agent_path=None)
    r = await conn.run("id; cat /etc/passwd", check=False)
    assert r.exit_status != 0 and "root:" not in r.stdout and "uid=" not in r.stdout
    assert "menu only" in r.stdout
    with pytest.raises((asyncssh.Error, OSError)):
        sftp = await conn.start_sftp_client()
        await sftp.listdir("/")
    with pytest.raises((asyncssh.Error, OSError)):
        await conn.forward_local_port("127.0.0.1", 0, "127.0.0.1", 22)
        await conn.create_connection(asyncssh.SSHTCPSession, "127.0.0.1", 22)
    conn.close()


async def test_wrong_or_unknown_password_refused_and_admin_console_key_only(gateways, bbs):
    ssh_port, admin_port, _ = gateways
    for port, user in ((ssh_port, "someone"), (admin_port, "admin")):
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect("127.0.0.1", port, username=user, password="hunter2", client_keys=[],
                                   known_hosts=None, agent_path=None)
    # Even a correct password never opens the admin console.
    k = gen_key()
    bbs.bootstrap_admin("pwadmin", None, None, pub(k))
    with bbs.db.session() as conn:
        from bbs_core.security import hash_password
        conn.execute("UPDATE users SET password_hash = ? WHERE username = 'pwadmin'", (hash_password("correct horse 1"),))
    with pytest.raises(asyncssh.PermissionDenied):
        await asyncssh.connect("127.0.0.1", admin_port, username="pwadmin", password="correct horse 1", client_keys=[],
                               known_hosts=None, agent_path=None)


async def test_register_and_log_in_with_password_when_device_has_no_key(gateways, bbs):
    ssh_port, _, _ = gateways
    # A fresh PC with no SSH key: 'ssh new@' still gets in (any password, or none).
    t = await connect(ssh_port, "new", password="whatever")
    await t.expect("Username")
    t.send("morag")
    for prompt in ("callsign", "Display name", "Contact"):
        await t.expect(prompt)
        t.send("")
    screen = await t.expect("Password: ")
    assert "Use this device's key" not in screen
    t.send("short")
    screen = await t.expect("Password: ")
    assert "At least 8 characters" in screen
    t.send("heather and thistle")
    await t.expect("Type it again: ")
    t.send("heather and thistle")
    screen = await t.expect("Submit this registration?")
    assert "your password" in screen and "heather" not in screen.split("Type it again")[-1]
    t.send("y")
    screen = await t.expect("-- press Enter --")
    assert "PENDING" in screen and "asked for your password" in screen
    t.send("")
    await t.close()

    # Wrong password: refused. Right password: in, read-only while pending.
    with pytest.raises(asyncssh.PermissionDenied):
        await connect(ssh_port, "morag", password="heather and thistlE")
    t = await connect(ssh_port, "morag", password="heather and thistle")
    screen = await t.expect("Selection:")
    assert "Account: PENDING" in screen
    await t.close()
    with bbs.db.session() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE username='morag'").fetchone()
    assert row[0].startswith("scrypt$") and "heather" not in row[0]


async def test_offered_but_unproven_key_is_not_registered(gateways, bbs):
    """A client that offers someone else's public key (without the private key) and then
    falls back to a password must not get that key put on its account."""
    ssh_port, _, _ = gateways
    victim = gen_key()
    from bbs_gateway.sshserver import BBSServer

    # Simulate the auth sequence directly on a server object.
    gw = gateways[2]
    srv = BBSServer(gw)
    srv.begin_auth("new")
    assert await srv.validate_public_key("new", asyncssh.import_public_key(pub(victim))) is True
    assert srv.proven_key is not None
    assert await srv.validate_password("new", "x") is True  # fell back to a password
    assert srv.proven_key is None and srv.proven_key_fp is None
    srv.begin_auth("new")
    assert await srv.validate_public_key("new", asyncssh.import_public_key(pub(victim))) is True
    assert srv.get_kbdint_challenge("new", "", "") is True
    assert srv.proven_key is None


async def test_lan_admin_gets_sysop_menu_in_bbs(gateways, bbs, admin_key, live_core):
    ssh_port, _, _ = gateways
    from bbs_core.config import SysopCfg

    bbs.cfg = bbs.cfg.model_copy(update={"sysop": SysopCfg(allowed_networks=["127.0.0.0/8"])})  # treat the test client as LAN
    t = await connect(ssh_port, "sysop1", admin_key)
    screen = await t.expect("Selection:")
    assert "S. Sysop Menu" in screen
    t.send("S")
    screen = await t.expect("Selection:")
    assert "BEACONNET - SYSOP MENU" in screen and "Pending Registrations" in screen and "Q. Back to the BBS" in screen
    t.send("Q")
    screen = await t.expect("Selection:")
    assert "Read Recent Bulletins" in screen  # back in the BBS
    await t.close()



async def test_admin_console_refuses_guests_and_users(gateways, bbs):
    _, admin_port, _ = gateways
    for user in ("guest", "new", "root"):
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect("127.0.0.1", admin_port, username=user, client_keys=[gen_key()],
                                   known_hosts=None, agent_path=None)


async def test_public_gateway_has_no_admin_menu(gateways, admin_key):
    ssh_port, _, _ = gateways
    t = await connect(ssh_port, "sysop1", admin_key)
    screen = await t.expect("Selection:")
    assert "ADMINISTRATION" not in screen and "Pending Registrations" not in screen
    t.send("10")  # admin menu's 'Configuration' number
    screen = await t.expect("Selection:")
    assert "Choose one of the options" in screen
    await t.close()


async def test_disconnect_mid_compose_is_harmless(gateways, admin_key, bbs):
    ssh_port, _, _ = gateways
    t = await connect(ssh_port, "sysop1", admin_key)
    await t.expect("Selection:")
    t.send("3")
    await t.expect("Board:")
    t.send("GENERAL")
    await t.expect("Subject:")
    t.send("Never finished")
    await t.expect("/EX")
    t.send("half a message")
    await asyncio.sleep(0.2)
    await t.close()  # drop the connection mid-message
    await asyncio.sleep(0.5)
    with bbs.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE subject = 'Never finished'").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The gateway ended the session in core when the connection dropped.
        live = conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
        assert live == 0


async def test_connection_limit_per_ip(gateways):
    ssh_port, _, gw = gateways
    gw.cfg.max_per_ip = 2
    conns = [await connect(ssh_port, "guest") for _ in range(2)]
    for t in conns:
        await t.expect("Selection:")
    with pytest.raises(asyncssh.PermissionDenied):
        await asyncssh.connect("127.0.0.1", ssh_port, username="guest", client_keys=[], known_hosts=None, agent_path=None)
    for t in conns:
        await t.close()


async def test_escape_sequences_not_relayed(gateways, admin_key, bbs):
    ssh_port, _, _ = gateways
    t = await connect(ssh_port, "sysop1", admin_key)
    await t.expect("Selection:")
    t.send("3")
    await t.expect("Board:")
    t.send("GENERAL")
    await t.expect("Subject:")
    t.send("Esc \x1b[31mred\x1b[0m title")
    await t.expect("/EX")
    t.send("body \x1b]0;owned\x07 text")
    t.send("/EX")
    await t.expect("Send this bulletin?")
    t.send("y")
    screen = await t.expect("Selection:")
    number = screen.split("Posted as message #")[1].split()[0]
    await t.close()
    g = await connect(ssh_port, "guest")
    await g.expect("Selection:")
    g.send(f"R {number}")
    screen = await g.expect("Selection:")
    assert "\x1b" not in screen and "\x07" not in screen
    assert "red" in screen and "owned" in screen
    await g.close()


async def test_member_list_in_mail(gateways, admin_key, active_user, bbs):
    ssh_port, _, _ = gateways
    t = await connect(ssh_port, "sysop1", admin_key)
    await t.expect("Selection:")
    t.send("5")
    await t.expect("M=members")
    t.send("M")
    await t.expect("Enter = everyone):")
    t.send("ali")
    screen = await t.expect("Write to")
    assert "alice" in screen and "Address book" in screen and "(1 matching)" in screen
    t.send("")
    await t.expect("M=members")
    t.send("C")
    await t.expect("? for the member list")
    t.send("?")
    await t.expect("Enter = everyone):")
    t.send("")
    screen = await t.expect("? for the member list")
    assert "alice" in screen
    t.send("alice")
    await t.expect("Subject:")
    t.send("Hello from the list")
    await t.expect("/EX")
    t.send("hi alice")
    t.send("/EX")
    screen = await t.expect("M=members")
    assert "Sent to alice" in screen
    await t.close()


def _rows(text: str) -> list[str]:
    import re
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "")
    return text.split("\n")


async def test_commodore_64_forty_columns(gateways, admin_key, active_user, ssh_api, bbs):
    ssh_port, _, _ = gateways
    _, _, s_alice = active_user
    ssh_api.call("POST", "/v1/messages", s_alice, json={
        "board": "GENERAL", "subject": "Café net tonight — “bring” a very long subject line here",
        "body": "A long body line that is certainly wider than forty columns and must wrap neatly on a C64."})
    # His C64 SSH client says it's a C64 with a 40x25 screen.
    t = await connect(ssh_port, "sysop1", admin_key, term_type="c64", size=(40, 25))
    await t.expect("Selection:")
    t.send("1")
    await t.expect("Q=back:")
    t.send("Q")
    await t.expect("Selection:")
    t.send("8")                                   # help: paged, long lines reflowed
    for _ in range(6):
        try:
            await t.expect("Q=stop --", timeout=2)
        except AssertionError:
            break
        t.send("")
    await t.expect("Selection:")
    t.send("5")
    await t.expect("Q=back:")
    t.send("M")
    await t.expect("everyone):")
    t.send("")
    await t.expect("Enter=back):")
    t.send("")
    await t.expect("Q=back:")
    t.send("Q")
    await t.expect("Selection:")
    t.send("W")                                   # switch to 80 columns by hand
    screen = await t.expect("Selection:")
    assert "Screen width: 80 columns." in screen
    await t.close()
    narrow = t.all.split("Screen width: 80")[0]
    too_wide = [r for r in _rows(narrow) if len(r) > 39]
    assert not too_wide, too_wide
    assert narrow.isascii() and "\x1b[1m" not in narrow          # plain ASCII, no ANSI for a C64
    assert "Cafe net" in narrow and "W. 80 columns" in narrow and "112, 911" in narrow


async def test_narrow_terminal_detected_by_size(gateways, admin_key):
    ssh_port, _, _ = gateways
    t = await connect(ssh_port, "sysop1", admin_key, pty=True, size=(40, 24))
    screen = await t.expect("Selection:")
    assert "W. 80 columns" in screen and all(len(r) <= 39 for r in _rows(screen))
    t.send("Q")
    await t.close()
    t = await connect(ssh_port, "sysop1", admin_key, pty=True, size=(100, 30))
    screen = await t.expect("Selection:")
    assert "W. 40 columns" in screen
    t.send("Q")
    await t.close()
