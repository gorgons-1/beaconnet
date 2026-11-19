"""Peer cards and peers.yaml editing (used by scripts/add-peer.sh and remove-peer.sh).

A peer card is one line that a sysop gives to a neighbouring node:

    SCOTBBS-PEER/1 <NODE-ID> <host or -> <port> ssh-ed25519 <key> <node name ...>

'-' as the host means "I'm behind NAT, I will dial you". The key is the node's
public node key: it encrypts the link and proves which node is on the other end.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from .config import ConfigError, PeerCfg, PeersFile

CARD_TAG = "SCOTBBS-PEER/1"

HEADER = """\
# Peer BBS nodes this node exchanges messages with (bbs-sync).
# Managed by scripts/add-peer.sh and scripts/remove-peer.sh; editing by hand is fine too.
# Restart after a change:  docker compose restart bbs-core bbs-sync
#
#   id:          the peer's node id (BBS_NODE_ID on that node)
#   address:     host/IP this node dials; null = the peer dials us (it's behind NAT)
#   port:        the peer's sync port (BBS_SYNC_PORT, default 2224)
#   public_key:  the peer's node key from its peer card - pinned, both directions
#   enabled:     false = keep the entry but don't link

"""


def parse_card(line: str) -> PeerCfg:
    parts = line.strip().split()
    if len(parts) < 6 or parts[0] != CARD_TAG:
        raise ConfigError(f"not a peer card (it should start with {CARD_TAG})")
    _, node_id, host, port, ktype, key, *name = parts
    try:
        return PeerCfg(id=node_id.upper(), name=" ".join(name) or node_id.upper(),
                       address=None if host == "-" else host, port=int(port), public_key=f"{ktype} {key}")
    except (ValidationError, ValueError) as exc:
        raise ConfigError(f"peer card is not valid: {exc}") from exc


def make_card(node_id: str, host: str | None, port: int, public_key: str, name: str) -> str:
    ktype, key = public_key.split()[:2]
    return f"{CARD_TAG} {node_id} {host or '-'} {port} {ktype} {key} {name}".strip()


def load(path: Path) -> list[PeerCfg]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    try:
        return PeersFile.model_validate(data or {}).peers
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save(path: Path, peers: list[PeerCfg]) -> None:
    PeersFile(peers=peers)  # validates uniqueness
    body = yaml.safe_dump({"peers": [p.model_dump() for p in peers]}, sort_keys=False, default_flow_style=False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".peers-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(HEADER + body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def add(path: Path, card: str, own_id: str, own_key: str | None = None) -> tuple[PeerCfg, bool]:
    """Add or update the peer described by a card. Returns (peer, replaced_existing)."""
    peer = parse_card(card)
    if peer.id == own_id:
        raise ConfigError("that is this node's own peer card")
    if own_key and peer.public_key == own_key.split()[0] + " " + own_key.split()[1]:
        raise ConfigError("that card carries this node's own key")
    peers = load(path)
    replaced = any(p.id == peer.id for p in peers)
    others = [p for p in peers if p.id != peer.id]
    if any(p.public_key == peer.public_key for p in others):
        raise ConfigError("another peer already uses that key")
    save(path, others + [peer])
    return peer, replaced


def remove(path: Path, node_id: str) -> bool:
    peers = load(path)
    keep = [p for p in peers if p.id != node_id.upper()]
    if len(keep) == len(peers):
        return False
    save(path, keep)
    return True
