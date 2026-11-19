"""Access and authentication rules (acceptance criteria 'Access and authentication')."""

from conftest import approve, gen_key, pub, register, user_id


def test_guest_can_read_public_bulletins(ssh_api, active_user):
    _, _, alice = active_user
    r = ssh_api.call("POST", "/v1/messages", alice, json={"board": "GENERAL", "subject": "Hello", "body": "First post"})
    assert r.status_code == 200, r.text
    n = r.json()["number"]
    g = ssh_api.guest()
    boards = ssh_api.call("GET", "/v1/boards", g).json()["boards"]
    assert {"ANNOUNCEMENTS", "GENERAL", "SCOTLAND", "EMERGENCY", "TECHNICAL", "RADIO", "EQUIPMENT", "LOCAL-NODES"} <= {b["name"] for b in boards}
    assert ssh_api.call("GET", "/v1/messages", g, params={"board": "GENERAL"}).json()["messages"][0]["number"] == n
    m = ssh_api.call("GET", f"/v1/messages/{n}", g).json()
    assert m["body"] == "First post"


def test_guest_cannot_post_or_mail(ssh_api):
    g = ssh_api.guest()
    r = ssh_api.call("POST", "/v1/messages", g, json={"board": "GENERAL", "subject": "x", "body": "y"})
    assert r.status_code == 403
    r = ssh_api.call("POST", "/v1/mail", g, json={"to": "sysop1", "subject": "x", "body": "y"})
    assert r.status_code == 403
    assert ssh_api.call("GET", "/v1/mail", g).status_code == 403


def test_registration_creates_pending_account(ssh_api, admin_api, admin_session):
    out = register(ssh_api, "bob", gen_key(), "MM0BOB")
    assert out["status"] == "PENDING"
    uid = user_id(admin_api, admin_session, "bob")
    u = admin_api.call("GET", f"/v1/admin/users/{uid}", admin_session).json()
    assert u["status"] == "PENDING" and u["callsign"] == "MM0BOB" and not u["callsign_verified"]
    assert u["registrations"][0]["decision"] is None


def test_pending_account_can_read_but_not_post(ssh_api):
    key = gen_key()
    register(ssh_api, "carol", key)
    s = ssh_api.login("carol", key).json()["token"]
    me = ssh_api.call("GET", "/v1/sessions/me", s).json()
    assert me["status"] == "PENDING" and me["can_post"] is False
    assert ssh_api.call("GET", "/v1/messages", s).status_code == 200
    r = ssh_api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": "x", "body": "y"})
    assert r.status_code == 403


def test_admin_approval_allows_posting(ssh_api, admin_api, admin_session):
    key = gen_key()
    register(ssh_api, "dave", key)
    s = ssh_api.login("dave", key).json()["token"]
    approve(admin_api, admin_session, "dave")
    # Same session: status is re-read on every request, no reconnect needed.
    r = ssh_api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": "Approved", "body": "Now I can post"})
    assert r.status_code == 200, r.text


def test_suspended_user_cannot_post(ssh_api, admin_api, admin_session, active_user):
    name, key, s = active_user
    uid = user_id(admin_api, admin_session, name)
    r = admin_api.call("POST", f"/v1/admin/users/{uid}/status", admin_session, json={"action": "suspend", "reason": "test"})
    assert r.status_code == 200
    # Existing session was ended by the suspension.
    assert ssh_api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": "x", "body": "y"}).status_code == 401
    # A new login gets read-only access only.
    s2 = ssh_api.login(name, key).json()["token"]
    assert ssh_api.call("POST", "/v1/messages", s2, json={"board": "GENERAL", "subject": "x", "body": "y"}).status_code == 403
    assert ssh_api.call("POST", "/v1/mail", s2, json={"to": "sysop1", "subject": "x", "body": "y"}).status_code == 403


def test_rejected_user_cannot_log_in(ssh_api, admin_api, admin_session):
    key = gen_key()
    register(ssh_api, "eve", key)
    uid = user_id(admin_api, admin_session, "eve")
    admin_api.call("POST", f"/v1/admin/users/{uid}/status", admin_session, json={"action": "reject"})
    assert ssh_api.login("eve", key).status_code == 403


def test_no_automatic_approval(ssh_api, bbs):
    for i in range(3):
        register(ssh_api, f"user{i}x", gen_key(), ip=f"10.1.0.{i}")
    with bbs.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE status='ACTIVE' AND role='user'").fetchone()[0] == 0


def test_wrong_key_or_unknown_user_rejected(ssh_api, active_user):
    name, _, _ = active_user
    assert ssh_api.login(name, gen_key()).status_code == 401
    assert ssh_api.login("nobody", gen_key()).status_code == 401
    r = ssh_api.call("POST", "/v1/auth/ssh-key/check", json={"username": name, "public_key": pub(gen_key())})
    assert r.json() == {"accepted": False}


def test_gateway_cannot_choose_identity(ssh_api, active_user):
    """Unknown fields (e.g. a user_id or role claimed by a gateway) are refused outright."""
    name, key, s = active_user
    r = ssh_api.call("POST", "/v1/sessions/ssh-key",
                     json={"username": "sysop1", "public_key": pub(key), "client_ip": "1.2.3.4"})
    assert r.status_code == 401  # alice's key does not make you sysop1
    r = ssh_api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": "x", "body": "y", "author": "sysop1"})
    assert r.status_code == 422


def test_public_gateway_cannot_reach_admin_api(ssh_api, admin_key):
    # Even the administrator's own key, via the public gateway, gets an ordinary session.
    s = ssh_api.login("sysop1", admin_key).json()
    assert s["kind"] == "user"
    for path in ("/v1/admin/users", "/v1/admin/audit", "/v1/admin/health", "/v1/admin/config"):
        assert ssh_api.call("GET", path, s["token"]).status_code == 403


def test_admin_gateway_requires_admin_role(admin_api, ssh_api, active_user):
    name, key, _ = active_user
    assert admin_api.login(name, key).status_code == 403
    r = admin_api.call("POST", "/v1/auth/ssh-key/check", json={"username": name, "public_key": pub(key)})
    assert r.json() == {"accepted": False}
    # Guests can't exist on the admin gateway.
    assert admin_api.call("POST", "/v1/sessions/guest", json={}).status_code == 403


def test_sessions_bound_to_their_gateway(ssh_api, admin_api, admin_session):
    # An admin session token presented through the public gateway is rejected.
    assert ssh_api.call("GET", "/v1/admin/users", admin_session).status_code in (401, 403)
    assert ssh_api.call("GET", "/v1/sessions/me", admin_session).status_code == 401


def test_key_revocation_ends_access(ssh_api, admin_api, admin_session, active_user):
    name, key, s = active_user
    keys = ssh_api.call("GET", "/v1/account/keys", s).json()["keys"]
    # Only key: owner can't revoke it (would lock out); admin can.
    assert ssh_api.call("DELETE", f"/v1/account/keys/{keys[0]['id']}", s).status_code == 409
    assert admin_api.call("DELETE", f"/v1/admin/keys/{keys[0]['id']}", admin_session).status_code == 200
    assert ssh_api.call("GET", "/v1/sessions/me", s).status_code == 401
    assert ssh_api.login(name, key).status_code == 401


def test_key_rotation(ssh_api, active_user):
    name, old, s = active_user
    new = gen_key()
    r = ssh_api.call("POST", "/v1/account/keys", s, json={"public_key": pub(new)})
    assert r.status_code == 200
    old_id = ssh_api.call("GET", "/v1/account/keys", s).json()["keys"][0]["id"]
    s_new = ssh_api.login(name, new).json()["token"]
    assert ssh_api.call("DELETE", f"/v1/account/keys/{old_id}", s_new).status_code == 200
    assert ssh_api.login(name, old).status_code == 401
    assert ssh_api.login(name, new).status_code == 200


def test_impersonation_blocked(ssh_api, active_user):
    g = ssh_api.guest("10.9.9.9")
    # alice holds callsign GM4AAA: nobody else may claim it, or use it (or 'alice') as a username.
    cases = [
        {"username": "alice2", "callsign": "GM4AAA"},
        {"username": "gm4aaa"},
        {"username": "ALICE"},
        {"username": "gm4zzz"},  # looks like a callsign that isn't theirs
        {"username": "sysop"},  # reserved
    ]
    for case in cases:
        r = ssh_api.call("POST", "/v1/registrations", g, json={**case, "public_key": pub(gen_key())})
        assert r.status_code in (400, 409), (case, r.text)


def test_same_key_cannot_register_twice(ssh_api):
    key = gen_key()
    register(ssh_api, "frank", key, ip="10.3.0.1")
    g = ssh_api.guest("10.3.0.2")
    r = ssh_api.call("POST", "/v1/registrations", g, json={"username": "frank2", "public_key": pub(key)})
    assert r.status_code == 409


def test_service_token_required(client):
    assert client.post("/v1/sessions/guest", json={}).status_code == 401
    assert client.post("/v1/sessions/guest", json={}, headers={"X-Service-Token": "x" * 40}).status_code == 401
    assert client.get("/v1/boards").status_code == 401


def test_session_token_required(ssh_api):
    assert ssh_api.call("GET", "/v1/boards").status_code == 401
    assert ssh_api.call("GET", "/v1/boards", "not-a-real-session").status_code == 401


def test_rate_limits(ssh_api):
    g = ssh_api.guest("10.5.5.5")
    codes = []
    for i in range(5):
        r = ssh_api.call("POST", "/v1/registrations", g, json={"username": f"spam{i}a", "public_key": pub(gen_key())})
        codes.append(r.status_code)
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_auth_rate_limit(bbs, ssh_api):
    key = pub(gen_key())
    codes = [ssh_api.call("POST", "/v1/auth/ssh-key/check", json={"username": "x", "public_key": key, "client_ip": "10.6.6.6"}).status_code
             for _ in range(bbs.cfg.rate_limits.auth_attempts_per_ip_per_min + 2)]
    assert codes[-1] == 429 and codes[0] == 200


def test_admin_actions_are_audited(admin_api, admin_session, ssh_api):
    register(ssh_api, "gina", gen_key())
    uid = user_id(admin_api, admin_session, "gina")
    admin_api.call("POST", f"/v1/admin/users/{uid}/status", admin_session, json={"action": "approve"})
    admin_api.call("POST", f"/v1/admin/users/{uid}/status", admin_session, json={"action": "approve"})  # 2nd: conflict
    entries = admin_api.call("GET", "/v1/admin/audit", admin_session, params={"limit": 50}).json()["entries"]
    approvals = [e for e in entries if e["action"] == "user_approve"]
    assert {e["outcome"] for e in approvals} == {"success", "denied"}
    assert all(e["actor_name"] == "sysop1" and e["ts"].endswith("Z") for e in approvals)


def test_admin_cannot_change_own_status(admin_api, admin_session, bbs):
    uid = user_id(admin_api, admin_session, "sysop1")
    assert admin_api.call("POST", f"/v1/admin/users/{uid}/status", admin_session, json={"action": "suspend"}).status_code == 403
