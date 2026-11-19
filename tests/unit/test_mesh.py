"""Mesh rules in bbs-core: queueing, dedup, loops, hop limit, relaying, remote mail, receipts."""

import copy
import json

import pytest

from conftest import ADMIN_TOKEN, SSH_TOKEN, SYNC_TOKEN, Api, gen_key, make_bbs, make_env
from bbs_core.config import ConfigError
from bbs_core.mesh import payload_hash


def deliver(src, dst) -> list[dict]:
    """What bbs-sync does for one direction of a link: send src's queue for dst."""
    box = src.bbs.mesh.outbox(dst.id, 200)
    results = [dst.bbs.mesh.receive(src.id, item["envelope"]) for item in box["items"]]
    src.bbs.mesh.results(dst.id, [i["qid"] for i in box["items"]], [])
    return results


def post(api: Api, session: str, board="GENERAL", subject="Hello mesh", body="Testing 1 2 3", **kw):
    r = api.call("POST", "/v1/messages", session, json={"board": board, "subject": subject, "body": body, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def messages(node, board="GENERAL"):
    with node.bbs.db.session() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT m.* FROM messages m JOIN boards b ON b.id = m.board_id WHERE b.name = ? ORDER BY m.id", (board,))]


@pytest.fixture
def pair(mesh):
    n = mesh("NODE-A", "NODE-B")
    a, b = n["NODE-A"], n["NODE-B"]
    a.link(b)
    b.link(a)
    a.build()
    b.build()
    return a, b


def test_bulletin_is_queued_and_delivered_once(pair):
    a, b = pair
    api, s = a.user("alice", "GM4AAA")
    out = post(api, s)
    assert out["forwarded_to"] == 1
    r = deliver(a, b)
    assert r[0]["status"] == "accepted" and r[0]["number"] == 1
    got = messages(b)[0]
    assert got["author_name"] == "alice@NODE-A" and got["origin_node"] == "NODE-A" and got["mid"] == out["mid"]
    assert json.loads(got["route"]) == ["NODE-A", "NODE-B"] and got["hop_count"] == 1
    assert got["author_callsign"] == "GM4AAA"
    # queue row is acked; nothing goes back to A (A is in the route)
    assert a.bbs.mesh.outbox("NODE-B", 10)["items"] == []
    assert b.bbs.mesh.outbox("NODE-A", 10)["items"] == []


def test_duplicate_loop_and_own_echo(pair):
    a, b = pair
    api, s = a.user("alice")
    post(api, s)
    env = a.bbs.mesh.outbox("NODE-B", 10)["items"][0]["envelope"]
    assert b.bbs.mesh.receive("NODE-A", env)["status"] == "accepted"
    assert b.bbs.mesh.receive("NODE-A", env)["status"] == "duplicate"
    # the same envelope coming back to its origin
    assert a.bbs.mesh.receive("NODE-B", env)["status"] == "duplicate"
    # an envelope that already passed through B
    looped = dict(copy.deepcopy(env), route=["NODE-A", "NODE-B"], hop_count=1)
    assert b.bbs.mesh.receive("NODE-A", looped)["status"] == "duplicate"
    assert len(messages(b)) == 1


@pytest.mark.parametrize("change, reason", [
    (lambda e: e["payload"].__setitem__("body", "tampered"), "payload hash mismatch"),
    (lambda e: e.__setitem__("mid", "nothex@NODE-A"), "bad message id"),
    (lambda e: e.__setitem__("mid", "0" * 32 + "@NODE-Z"), "bad message id"),
    (lambda e: e.__setitem__("route", ["NODE-Z"]), "bad route"),
    (lambda e: e.__setitem__("v", 2), "unsupported envelope version"),
    (lambda e: e.__setitem__("kind", "shell"), "unknown kind"),
    (lambda e: e.__setitem__("created_at", "2999-01-01T00:00:00Z"), "dated in the future"),
    (lambda e: e.__setitem__("priority", "URGENT!!"), "bad priority"),
    (lambda e: e.__setitem__("hop_count", "1"), "bad hop count"),
])
def test_bad_envelopes_rejected(pair, change, reason):
    a, b = pair
    api, s = a.user("alice")
    post(api, s)
    env = copy.deepcopy(a.bbs.mesh.outbox("NODE-B", 10)["items"][0]["envelope"])
    change(env)
    assert b.bbs.mesh.receive("NODE-A", env) == {"status": "rejected", "reason": reason}
    assert messages(b) == []


def test_unknown_or_disabled_peer_refused(pair):
    a, b = pair
    api, s = a.user("alice")
    post(api, s)
    env = a.bbs.mesh.outbox("NODE-B", 10)["items"][0]["envelope"]
    with pytest.raises(Exception) as e:
        b.bbs.mesh.receive("NODE-X", env)
    assert getattr(e.value, "code", "") == "forbidden"


def test_expired_and_unknown_board(pair):
    a, b = pair
    api, s = a.user("alice")
    post(api, s)
    env = copy.deepcopy(a.bbs.mesh.outbox("NODE-B", 10)["items"][0]["envelope"])
    env["expires_at"] = "2020-01-01T00:00:00Z"
    assert b.bbs.mesh.receive("NODE-A", env)["status"] == "expired"
    env = copy.deepcopy(a.bbs.mesh.outbox("NODE-B", 10)["items"][0]["envelope"])
    env["destination"] = {"board": "NOWHERE"}
    assert b.bbs.mesh.receive("NODE-A", env) == {"status": "rejected", "reason": "no such board here"}


def test_relay_through_middle_node_and_hop_limit(mesh):
    n = mesh("NODE-A", "NODE-B", "NODE-C")
    a, b, c = n["NODE-A"], n["NODE-B"], n["NODE-C"]
    a.link(b); b.link(a); b.link(c); c.link(b)  # noqa: E702  a line: A - B - C
    for x in (a, b, c):
        x.build()
    api, s = a.user("alice")
    post(api, s, board="EMERGENCY", subject="Road closed", body="A9 closed at Dalwhinnie", area="Highland",
         priority="EMERGENCY")
    deliver(a, b)
    assert deliver(b, c)[0]["status"] == "accepted"
    got = messages(c, "EMERGENCY")[0]
    assert json.loads(got["route"]) == ["NODE-A", "NODE-B", "NODE-C"] and got["hop_count"] == 2
    assert got["priority"] == "EMERGENCY" and got["area"] == "Highland" and got["expires_at"]
    # hop limit: an envelope already at the limit is refused, not stored or relayed
    env = copy.deepcopy(c.bbs.mesh.outbox("NODE-B", 10)["items"] or [])
    assert env == []  # C only links back to B, who is in the route
    raw = b.bbs.mesh.envelope("bulletin", "f" * 32 + "@NODE-B", {"board": "GENERAL"},
                              {"author": "x", "subject": "s", "body": "b"}, "2026-01-01T00:00:00Z")
    raw.update(hop_count=8, hop_limit=8, route=["NODE-B"] + [f"NODE-Q{i}" for i in range(8)])
    assert c.bbs.mesh.receive("NODE-B", raw)["reason"] == "hop limit reached"


def test_unshared_board_stays_local(mesh, tmp_path):
    n = mesh("NODE-A", "NODE-B")
    a, b = n["NODE-A"], n["NODE-B"]
    a.link(b)
    b.link(a)
    a.build()
    with a.bbs.db.session() as conn:
        conn.execute("UPDATE boards SET forward = 0 WHERE name = 'LOCAL-NODES'")
    api, s = a.user("alice")
    assert post(api, s, board="LOCAL-NODES")["forwarded_to"] == 0


def test_remote_mail_and_delivery_receipt(pair):
    a, b = pair
    api_a, alice = a.user("alice")
    api_b, bob = b.user("bob")
    r = api_a.call("POST", "/v1/mail", alice, json={"to": "bob@node-b", "subject": "Hi Bob", "body": "Over the mesh"})
    assert r.status_code == 200 and r.json()["delivery_state"] == "QUEUED" and r.json()["to"] == "bob@NODE-B"
    pm = r.json()["id"]
    assert deliver(a, b)[0] == {"status": "accepted", "delivery": "DELIVERED"}
    inbox = api_b.call("GET", "/v1/mail", bob).json()["messages"]
    assert inbox[0]["from"] == "alice@NODE-A" and inbox[0]["subject"] == "Hi Bob"
    assert deliver(b, a)[0]["status"] == "accepted"     # the receipt
    assert api_a.call("GET", f"/v1/mail/{pm}", alice).json()["delivery_state"] == "DELIVERED"
    # reply goes back the same way
    r = api_b.call("POST", "/v1/mail", bob, json={"to": inbox[0]["from"], "subject": "Re: Hi", "body": "Got it"})
    assert r.json()["delivery_state"] == "QUEUED"
    deliver(b, a)
    assert api_a.call("GET", "/v1/mail", alice).json()["messages"][0]["from"] == "bob@NODE-B"


def test_remote_mail_to_unknown_user_fails_back(pair):
    a, b = pair
    api_a, alice = a.user("alice")
    pm = api_a.call("POST", "/v1/mail", alice, json={"to": "nobody@NODE-B", "subject": "?", "body": "?"}).json()["id"]
    assert deliver(a, b)[0]["delivery"] == "FAILED"
    deliver(b, a)
    assert api_a.call("GET", f"/v1/mail/{pm}", alice).json()["delivery_state"] == "FAILED"


def test_remote_mail_needs_a_peer(bbs, ssh_api, active_user):
    _, _, s = active_user
    r = ssh_api.call("POST", "/v1/mail", s, json={"to": "bob@NODE-B", "subject": "x", "body": "y"})
    assert r.status_code == 503 or r.json()["error"]["code"] == "not_available"


def test_relay_of_mail_for_a_far_node(mesh):
    n = mesh("NODE-A", "NODE-B", "NODE-C")
    a, b, c = n["NODE-A"], n["NODE-B"], n["NODE-C"]
    a.link(b); b.link(a); b.link(c); c.link(b)  # noqa: E702
    for x in (a, b, c):
        x.build()
    api_a, alice = a.user("alice")
    api_c, carol = c.user("carol")
    pm = api_a.call("POST", "/v1/mail", alice, json={"to": "carol@NODE-C", "subject": "Far", "body": "Two hops"}).json()["id"]
    assert deliver(a, b)[0] == {"status": "accepted", "relayed": True, "forwarded_to": 1}
    with b.bbs.db.session() as conn:  # the relay doesn't put it in anyone's mailbox
        assert conn.execute("SELECT COUNT(*) FROM private_messages").fetchone()[0] == 0
    assert deliver(b, c)[0]["delivery"] == "DELIVERED"
    assert api_c.call("GET", "/v1/mail", carol).json()["messages"][0]["from"] == "alice@NODE-A"
    deliver(c, b)
    deliver(b, a)
    assert api_a.call("GET", f"/v1/mail/{pm}", alice).json()["delivery_state"] == "DELIVERED"


def test_failed_sends_back_off(pair):
    a, b = pair
    api, s = a.user("alice")
    post(api, s)
    qid = a.bbs.mesh.outbox("NODE-B", 10)["items"][0]["qid"]
    a.bbs.mesh.results("NODE-B", [], [{"qid": qid, "error": "busy"}])
    assert a.bbs.mesh.outbox("NODE-B", 10)["items"] == []   # not due again yet
    with a.bbs.db.session() as conn:
        row = conn.execute("SELECT attempts, last_error, state FROM forward_queue WHERE id = ?", (qid,)).fetchone()
    assert tuple(row) == (1, "busy", "QUEUED")


def test_sync_token_is_walled_off(pair):
    a, _ = pair
    c = a.client()
    sync, ssh = Api(c, SYNC_TOKEN), Api(c, SSH_TOKEN)
    assert sync.call("POST", "/v1/sessions/guest", json={}).status_code == 403
    assert sync.call("GET", "/v1/boards").status_code == 403
    assert ssh.call("GET", "/v1/sync/config").status_code == 403
    assert Api(c, ADMIN_TOKEN).call("GET", "/v1/sync/outbox/NODE-B").status_code == 403
    cfg = sync.call("GET", "/v1/sync/config").json()
    assert cfg["node_id"] == "NODE-A" and cfg["peers"][0]["id"] == "NODE-B"
    assert sync.call("GET", "/v1/sync/outbox/not a node").status_code == 422
    assert sync.call("POST", "/v1/sync/inbox/NODE-B", json=["not", "an", "object"]).status_code == 422
    assert sync.call("POST", "/v1/sync/peers/NODE-B/status", json={"state": "CONNECTED"}).status_code == 200


def test_peer_config_validation(tmp_path):
    k = gen_key().export_public_key("openssh").decode().strip()
    good = {"id": "NODE-B", "name": "B", "address": "b.example.org", "port": 2224, "public_key": k}
    make_bbs(make_env(tmp_path / "1", "NODE-A", [good]))
    for bad in ({**good, "public_key": "ssh-rsa AAAA"}, {**good, "address": "evil host; rm"},
                {**good, "id": "NODE-A"}):
        with pytest.raises(ConfigError):
            make_bbs(make_env(tmp_path / str(id(bad)), "NODE-A", [bad]))
    with pytest.raises(ConfigError):
        make_bbs(make_env(tmp_path / "dup", "NODE-A", [good, {**good, "id": "NODE-C"}]))


def test_peer_cards(tmp_path):
    from bbs_core import peers

    k = gen_key().export_public_key("openssh").decode().strip()
    card = peers.make_card("NODE-B", None, 2224, k + " comment", "Inverness Node")
    assert card.startswith("SCOTBBS-PEER/1 NODE-B - 2224 ssh-ed25519 ")
    f = tmp_path / "peers.yaml"
    f.write_text("peers: []\n")
    p, replaced = peers.add(f, card, "NODE-A")
    assert (p.id, p.address, p.name, replaced) == ("NODE-B", None, "Inverness Node", False)
    p, replaced = peers.add(f, card.replace(" - ", " b.example.org "), "NODE-A")
    assert p.address == "b.example.org" and replaced and len(peers.load(f)) == 1
    with pytest.raises(ConfigError):
        peers.add(f, card.replace("NODE-B", "NODE-A"), "NODE-A")
    with pytest.raises(ConfigError):
        peers.add(f, "hello world", "NODE-A")
    assert peers.remove(f, "node-b") and peers.load(f) == []


def test_admin_sees_peers_and_queue(pair):
    a, b = pair
    api, s = a.user("alice")
    post(api, s)
    adm = Api(a.client(), ADMIN_TOKEN)
    sess = adm.login("sysop1", a._admin).json()["token"]
    q = adm.call("GET", "/v1/admin/queues", sess).json()
    assert q["forwarding_enabled"] is True and q["waiting_per_peer"] == {"NODE-B": 1}
    nodes = {x["node_id"]: x for x in adm.call("GET", "/v1/admin/nodes", sess).json()["nodes"]}
    assert nodes["NODE-B"]["waiting"] == 1 and nodes["NODE-B"]["address"] == "127.0.0.1:2224"


def test_payload_hash_is_order_independent():
    assert payload_hash({"a": 1, "b": "é"}) == payload_hash({"b": "é", "a": 1})
