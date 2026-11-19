"""Bulletins, private mail, emergency traffic and input handling."""

import sqlite3

import pytest

from conftest import approve, gen_key, register, user_id


@pytest.fixture
def bob(ssh_api, admin_api, admin_session):
    key = gen_key()
    register(ssh_api, "bob", key, "MM0BOB", ip="10.0.0.3")
    approve(admin_api, admin_session, "bob")
    return ssh_api.login("bob", key).json()["token"]


def post(api, s, **kw):
    body = {"board": "GENERAL", "subject": "Subject", "body": "Body text", **kw}
    return api.call("POST", "/v1/messages", s, json=body)


def test_post_and_read_bulletin(ssh_api, active_user):
    _, _, s = active_user
    r = post(ssh_api, s, subject="Net tonight", body="2m net at 20:00 on GB3XX")
    assert r.status_code == 200
    n, mid = r.json()["number"], r.json()["mid"]
    assert mid.endswith("@BEACON-HUB-01")
    m = ssh_api.call("GET", f"/v1/messages/{n}", s).json()
    for field in ("mid", "number", "origin_node", "author", "callsign", "board", "subject", "body", "created_at", "status"):
        assert field in m
    assert m["author"] == "alice" and m["callsign"] == "GM4AAA" and m["created_at"].endswith("Z")


def test_reply_links_to_parent(ssh_api, active_user, bob):
    _, _, s = active_user
    n = post(ssh_api, s).json()["number"]
    r = post(ssh_api, bob, subject="Re: Subject", body="Reply", reply_to=n)
    m = ssh_api.call("GET", f"/v1/messages/{r.json()['number']}", bob).json()
    assert m["parent_number"] == n


def test_announcements_admin_only(ssh_api, active_user, admin_key):
    _, _, s = active_user
    assert post(ssh_api, s, board="ANNOUNCEMENTS").status_code == 403
    admin_user_session = ssh_api.login("sysop1", admin_key).json()["token"]
    assert post(ssh_api, admin_user_session, board="ANNOUNCEMENTS").status_code == 200


def test_read_markers(ssh_api, active_user, bob):
    _, _, s = active_user
    n = post(ssh_api, s, subject="Unread test", body="hi").json()["number"]
    rows = ssh_api.call("GET", "/v1/messages", bob, params={"board": "GENERAL"}).json()["messages"]
    assert [m for m in rows if m["number"] == n][0]["read"] is False
    ssh_api.call("POST", f"/v1/messages/{n}/read", bob)
    rows = ssh_api.call("GET", "/v1/messages", bob, params={"board": "GENERAL"}).json()["messages"]
    assert [m for m in rows if m["number"] == n][0]["read"] is True
    boards = {b["name"]: b for b in ssh_api.call("GET", "/v1/boards", bob).json()["boards"]}
    assert boards["GENERAL"]["unread"] == 0


def test_search(ssh_api, active_user):
    _, _, s = active_user
    a = post(ssh_api, s, subject="Antenna for Skye", body="Looking for a 70cm yagi").json()["number"]
    post(ssh_api, s, board="RADIO", subject="Propagation", body="Good conditions up north").json()
    g = ssh_api.guest()
    assert [m["number"] for m in ssh_api.call("GET", "/v1/messages/search", g, params={"subject": "anten"}).json()["messages"]] == [a]
    assert [m["number"] for m in ssh_api.call("GET", "/v1/messages/search", g, params={"text": "yagi"}).json()["messages"]] == [a]
    assert ssh_api.call("GET", "/v1/messages/search", g, params={"number": a}).json()["messages"][0]["number"] == a
    assert len(ssh_api.call("GET", "/v1/messages/search", g, params={"sender": "GM4AAA"}).json()["messages"]) == 2
    assert len(ssh_api.call("GET", "/v1/messages/search", g, params={"board": "RADIO"}).json()["messages"]) == 1
    # FTS syntax and SQL in user input are just text.
    for nasty in ['"', "*", "NEAR(", "') OR 1=1 --", "subject:", "a AND OR NOT", "%_"]:
        r = ssh_api.call("GET", "/v1/messages/search", g, params={"subject": nasty})
        assert r.status_code in (200, 400), (nasty, r.text)


def test_duplicate_bulletin_rejected(ssh_api, active_user):
    _, _, s = active_user
    assert post(ssh_api, s, subject="Same", body="Same  text").status_code == 200
    r = post(ssh_api, s, subject="same", body="Same text")  # case/whitespace-insensitive
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate"


def test_duplicate_message_ids_rejected(bbs, ssh_api, active_user):
    _, _, s = active_user
    mid = post(ssh_api, s).json()["mid"]
    with bbs.db.session() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO messages(mid, origin_node, board_id, author_name, subject, body, content_hash, created_at, received_at, updated_at)"
            " VALUES (?, 'X', 1, 'x', 's', 'b', 'h', 't', 't', 't')", (mid,))


def test_private_mail_restricted(ssh_api, active_user, bob):
    _, _, alice = active_user
    r = ssh_api.call("POST", "/v1/mail", alice, json={"to": "bob", "subject": "Private", "body": "For bob only"})
    assert r.status_code == 200
    pm = r.json()["id"]
    assert ssh_api.call("GET", f"/v1/mail/{pm}", bob).json()["body"] == "For bob only"
    assert ssh_api.call("GET", f"/v1/mail/{pm}", alice).status_code == 200  # sender
    key = gen_key()
    register(ssh_api, "mallory", key, ip="10.0.0.9")
    m = ssh_api.login("mallory", key).json()["token"]
    assert ssh_api.call("GET", f"/v1/mail/{pm}", m).status_code == 403  # pending: no mail at all
    g = ssh_api.guest()
    assert ssh_api.call("GET", f"/v1/mail/{pm}", g).status_code == 403
    inbox = ssh_api.call("GET", "/v1/mail", bob).json()
    assert inbox["unread"] == 0 and inbox["messages"][0]["read"] is True


def test_private_mail_third_party_gets_not_found(ssh_api, admin_api, admin_session, active_user, bob):
    _, _, alice = active_user
    pm = ssh_api.call("POST", "/v1/mail", alice, json={"to": "bob", "subject": "P", "body": "secret"}).json()["id"]
    key = gen_key()
    register(ssh_api, "carl", key, ip="10.0.0.8")
    approve(admin_api, admin_session, "carl")
    carl = ssh_api.login("carl", key).json()["token"]
    r = ssh_api.call("GET", f"/v1/mail/{pm}", carl)
    assert r.status_code == 404
    assert ssh_api.call("POST", f"/v1/mail/{pm}/state", carl, json={"state": "DELETED"}).status_code == 404
    # No admin API reads private mail while privacy.admin_can_read_private_mail is false.
    assert admin_api.call("GET", f"/v1/mail/{pm}", admin_session).status_code == 404


def test_mail_folders_and_delete(ssh_api, active_user, bob):
    _, _, alice = active_user
    pm = ssh_api.call("POST", "/v1/mail", alice, json={"to": "bob", "subject": "Hi", "body": "x"}).json()["id"]
    assert ssh_api.call("GET", "/v1/mail", alice, params={"folder": "sent"}).json()["messages"][0]["id"] == pm
    ssh_api.call("POST", f"/v1/mail/{pm}/state", bob, json={"state": "ARCHIVED"})
    assert ssh_api.call("GET", "/v1/mail", bob).json()["messages"] == []
    assert ssh_api.call("GET", "/v1/mail", bob, params={"folder": "archive"}).json()["messages"][0]["id"] == pm
    ssh_api.call("POST", f"/v1/mail/{pm}/state", bob, json={"state": "DELETED"})
    ssh_api.call("POST", f"/v1/mail/{pm}/state", alice, json={"state": "DELETED"})
    assert ssh_api.call("GET", f"/v1/mail/{pm}", alice).status_code == 404


def test_mail_to_remote_node_not_yet(ssh_api, active_user):
    _, _, s = active_user
    r = ssh_api.call("POST", "/v1/mail", s, json={"to": "bob@BEACON-NORTH-01", "subject": "x", "body": "y"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_available"


def test_emergency_bulletins(ssh_api, active_user):
    _, _, s = active_user
    assert post(ssh_api, s, board="EMERGENCY", subject="No area", body="x").status_code == 400
    r1 = post(ssh_api, s, board="EMERGENCY", subject="Routine info", body="a", area="Fife")
    r2 = post(ssh_api, s, board="EMERGENCY", subject="Flooding A82", body="b", area="Highland", priority="EMERGENCY")
    r3 = post(ssh_api, s, board="EMERGENCY", subject="Power cut", body="c", area="Skye", priority="PRIORITY", expires_hours=2)
    assert all(r.status_code == 200 for r in (r1, r2, r3))
    data = ssh_api.call("GET", "/v1/messages", s, params={"board": "EMERGENCY"}).json()
    assert [m["priority"] for m in data["messages"]] == ["EMERGENCY", "PRIORITY", "ROUTINE"]
    assert "999" in data["disclaimer"]
    m = ssh_api.call("GET", f"/v1/messages/{r3.json()['number']}", s).json()
    assert m["area"] == "Skye" and m["expires_at"] and not m["expired"]
    # Priority on a normal board is ignored (can't jump queues elsewhere).
    n = post(ssh_api, s, subject="Not urgent", body="z", priority="EMERGENCY").json()["number"]
    assert ssh_api.call("GET", f"/v1/messages/{n}", s).json()["priority"] == "ROUTINE"
    assert post(ssh_api, s, board="EMERGENCY", subject="x", body="y", area="A", expires_hours=99999).status_code == 422


def test_invalid_input_handled_safely(ssh_api, active_user):
    _, _, s = active_user
    assert post(ssh_api, s, subject="", body="x").status_code == 400
    assert post(ssh_api, s, subject="x", body="").status_code == 400
    assert post(ssh_api, s, body="x" * 9000).status_code == 400
    assert post(ssh_api, s, board="NOPE").status_code == 404
    assert ssh_api.call("GET", "/v1/messages/abc", s).status_code == 422
    assert ssh_api.call("GET", "/v1/messages/999999", s).status_code == 404
    r = ssh_api.call("POST", "/v1/messages", s, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    r = ssh_api.call("POST", "/v1/messages", s, content=b"x" * 170000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert "error" in r.json()


def test_terminal_escapes_stripped(ssh_api, active_user):
    _, _, s = active_user
    evil = "Hi\x1b[2J\x1b]0;pwned\x07\x9b31m‮evil\x00"
    n = post(ssh_api, s, subject=evil, body=f"line1\r\n{evil}\n").json()["number"]
    m = ssh_api.call("GET", f"/v1/messages/{n}", s).json()
    for field in (m["subject"], m["body"]):
        assert "\x1b" not in field and "\x07" not in field and "\x9b" not in field and "‮" not in field and "\x00" not in field
    assert m["body"].startswith("line1\n")


def test_shell_metacharacters_are_plain_text(ssh_api, active_user):
    _, _, s = active_user
    payload = "$(touch /tmp/pwned); `id`; rm -rf / && echo | nc x 1 > /etc/passwd"
    n = post(ssh_api, s, subject=payload[:60], body=payload).json()["number"]
    assert ssh_api.call("GET", f"/v1/messages/{n}", s).json()["body"] == payload


def test_moderation(ssh_api, admin_api, admin_session, active_user):
    _, _, s = active_user
    n = post(ssh_api, s, subject="Spam", body="buy now").json()["number"]
    g = ssh_api.guest()
    r = admin_api.call("POST", f"/v1/admin/messages/{n}/moderate", admin_session, json={"action": "hide", "reason": "spam"})
    assert r.status_code == 200
    assert ssh_api.call("GET", f"/v1/messages/{n}", g).status_code == 404
    assert admin_api.call("GET", f"/v1/messages/{n}", admin_session).json()["status"] == "HIDDEN"
    admin_api.call("POST", f"/v1/admin/messages/{n}/moderate", admin_session, json={"action": "restore"})
    assert ssh_api.call("GET", f"/v1/messages/{n}", g).status_code == 200
    admin_api.call("POST", f"/v1/admin/messages/{n}/moderate", admin_session, json={"action": "delete"})
    assert ssh_api.call("GET", f"/v1/messages/{n}", g).status_code == 404


def test_board_management(ssh_api, admin_api, admin_session, active_user):
    _, _, s = active_user
    assert admin_api.call("POST", "/v1/admin/boards", admin_session, json={"action": "create", "name": "WX", "description": "Weather"}).status_code == 200
    assert admin_api.call("POST", "/v1/admin/boards", admin_session, json={"action": "rename", "name": "WX", "new_name": "WEATHER"}).status_code == 200
    assert post(ssh_api, s, board="WEATHER").status_code == 200
    assert admin_api.call("POST", "/v1/admin/boards", admin_session, json={"action": "archive", "name": "WEATHER"}).status_code == 200
    assert post(ssh_api, s, board="WEATHER").status_code == 404
    names = [b["name"] for b in ssh_api.call("GET", "/v1/boards", s).json()["boards"]]
    assert "WEATHER" not in names
    assert admin_api.call("POST", "/v1/admin/boards", admin_session, json={"action": "create", "name": "bad name!"}).status_code == 400


def test_mail_requires_active_recipient(ssh_api, active_user):
    _, _, s = active_user
    register(ssh_api, "pendy", gen_key(), ip="10.0.0.7")
    r = ssh_api.call("POST", "/v1/mail", s, json={"to": "pendy", "subject": "x", "body": "y"})
    assert r.status_code == 404


def test_admin_health_and_status(admin_api, admin_session, ssh_api):
    h = admin_api.call("GET", "/v1/admin/health", admin_session, params={"full": "true"}).json()
    assert h["database"]["integrity_check"] == "ok" and h["database"]["schema_version"] == 3
    assert admin_api.call("GET", "/v1/admin/queues", admin_session).json()["forwarding_enabled"] is False
    nodes = admin_api.call("GET", "/v1/admin/nodes", admin_session).json()["nodes"]
    assert nodes[0]["node_id"] == "BEACON-HUB-01" and nodes[0]["is_local"] == 1
    cfg = admin_api.call("GET", "/v1/admin/config", admin_session).json()
    assert "service_tokens" not in str(cfg).replace("services_with_tokens", "")
    info = ssh_api.call("GET", "/v1/system/info", ssh_api.guest()).json()
    assert info["node_id"] == "BEACON-HUB-01" and "999" in info["disclaimer"]


def test_forty_column_reflow_and_ascii():
    from bbs_gateway.term import to_ascii, vlen

    class P:                       # just enough of an asyncssh process for Term
        def get_terminal_type(self):
            return "c64"

        def get_terminal_size(self):
            return (0, 0, 0, 0)

    from bbs_gateway.term import Term, cols
    t = Term(P())
    assert t.width == 40 and cols() == 40 and t.ascii and not t.ansi
    out = t.fit("  " + "word " * 30 + "x" * 90)
    assert all(vlen(r) <= 39 for r in out.split("\n")) and out.count("x") == 90
    assert to_ascii("Café ‘q’ — £5 中") == "Cafe 'q' - L5 ?"
    t.set_width(80, fixed=True)
    assert cols() == 80 and t.fit("a " * 60).count("\n") == 0


def test_reply_subjects_keep_one_re(ssh_api, active_user, bob):
    from bbs_core.service import tidy_subject
    assert tidy_subject("Re: Re: RE:  test") == "Re: test"
    assert tidy_subject("re[2]: Re: net") == "Re: net"
    assert tidy_subject("Repeater news") == "Repeater news" and tidy_subject("Re:") == "Re:"
    _, _, alice = active_user
    r = ssh_api.call("POST", "/v1/mail", alice, json={"to": "bob", "subject": "Re: Re: Re: test", "body": "x"})
    assert r.status_code == 200
    assert ssh_api.call("GET", f"/v1/mail/{r.json()['id']}", alice).json()["subject"] == "Re: test"
