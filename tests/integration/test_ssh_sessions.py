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
    assert "SCOTLAND EMERGENCY PACKET RADIO BBS" in screen and "Node: SCOTLAND-HUB-01" in screen
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
    assert "999 or 112" in screen
    t.send("")
    await t.expect("Selection:")
    t.send("Q")
    await t.expect("73")
    await t.close()


async def test_full_registration_approval_and_posting(gateways, bbs):
    ssh_port, admin_port, _ = gateways
    user_key = gen_key()

    # 1. Register through the new@ login.
    t = await connect(ssh_port, "new")
    await t.expect("Username")
    t.send("hamish")
    await t.expect("callsign")
    t.send("gm7ham")
    await t.expect("Display name")
    t.send("Hamish")
    await t.expect("Contact")
    t.send("")
    await t.expect("> ")
    t.send(pub(user_key, "hamish@laptop"))
    await t.expect("Submit this registration?")
    t.send("y")
    screen = await t.expect("-- press Enter --")
    assert "PENDING" in screen and "SHA256:" in screen
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
    assert "SCOTLAND BBS - ADMINISTRATION" in screen and "1. Pending Registrations (1)" in screen
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


async def test_no_password_auth_offered(gateways):
    ssh_port, admin_port, _ = gateways
    for port, user in ((ssh_port, "someone"), (admin_port, "admin")):
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect("127.0.0.1", port, username=user, password="hunter2", client_keys=[],
                                   known_hosts=None, agent_path=None)


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
