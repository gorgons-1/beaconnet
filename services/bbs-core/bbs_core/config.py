"""Configuration: config/*.yaml + environment, validated at start-up (fail fast)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

NODE_ID_PATTERN = r"^[A-Z0-9][A-Z0-9-]{2,31}$"
BOARD_NAME_PATTERN = r"^[A-Z0-9][A-Z0-9_-]{1,23}$"


class ConfigError(Exception):
    """Raised for any invalid or missing configuration."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeCfg(_Strict):
    id: str = Field(pattern=NODE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=60)
    location: str = Field(default="", max_length=60)
    sysop: str = Field(default="", max_length=20)


class GuestCfg(_Strict):
    enabled: bool = True


class RegistrationCfg(_Strict):
    enabled: bool = True
    require_callsign: bool = False
    max_keys_per_user: int = Field(default=5, ge=1, le=20)


class SessionsCfg(_Strict):
    guest_idle_timeout_s: int = Field(default=600, ge=30, le=86400)
    user_idle_timeout_s: int = Field(default=1800, ge=30, le=86400)
    admin_idle_timeout_s: int = Field(default=900, ge=30, le=86400)
    session_ttl_hours: int = Field(default=12, ge=1, le=168)


class RateLimitsCfg(_Strict):
    auth_attempts_per_ip_per_min: int = Field(default=30, ge=1)
    registrations_per_ip_per_hour: int = Field(default=3, ge=1)
    registrations_global_per_hour: int = Field(default=30, ge=1)
    posts_per_user_per_hour: int = Field(default=30, ge=1)
    mail_per_user_per_hour: int = Field(default=60, ge=1)


class MessagesCfg(_Strict):
    subject_max: int = Field(default=80, ge=10, le=200)
    body_max_bytes: int = Field(default=8000, ge=100, le=65536)
    duplicate_window_hours: int = Field(default=72, ge=0, le=8760)
    emergency_default_expiry_hours: int = Field(default=72, ge=1)
    emergency_max_expiry_hours: int = Field(default=720, ge=1)
    page_size: int = Field(default=15, ge=5, le=50)


class PrivacyCfg(_Strict):
    admin_can_read_private_mail: bool = False


class RetentionCfg(_Strict):
    audit_days: int = Field(default=730, ge=0)
    session_days: int = Field(default=30, ge=1)
    rejected_registration_days: int = Field(default=90, ge=1)


class MeshCfg(_Strict):
    hop_limit: int = Field(default=8, ge=1, le=16)          # envelopes are dropped after this many hops
    max_envelopes_per_peer_per_hour: int = Field(default=2000, ge=10)
    forward_private_mail: bool = True                       # relay mail for other nodes through this one
    max_clock_skew_hours: int = Field(default=24, ge=1)     # refuse envelopes dated further in the future
    seen_days: int = Field(default=60, ge=7)                # how long message ids are remembered for dedup


class LoggingCfg(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class BoardCfg(_Strict):
    name: str = Field(pattern=BOARD_NAME_PATTERN)
    description: str = Field(default="", max_length=80)
    post: Literal["users", "admins"] = "users"
    public: bool = True
    emergency: bool = False
    forward: bool = True     # share this board's bulletins with peer nodes


PEER_KEY_PATTERN = r"^ssh-ed25519 [A-Za-z0-9+/]{68}$"


class PeerCfg(_Strict):
    id: str = Field(pattern=NODE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=60)
    # Host or IP this node dials. Leave empty when the peer is behind NAT and dials us.
    address: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9.:\[\]_-]+$")
    port: int = Field(default=2224, ge=1, le=65535)
    public_key: str = Field(pattern=PEER_KEY_PATTERN)   # the peer's node key: proves who it is, both ways
    enabled: bool = True


class BBSConfig(_Strict):
    node: NodeCfg
    guest: GuestCfg = GuestCfg()
    registration: RegistrationCfg = RegistrationCfg()
    sessions: SessionsCfg = SessionsCfg()
    rate_limits: RateLimitsCfg = RateLimitsCfg()
    messages: MessagesCfg = MessagesCfg()
    privacy: PrivacyCfg = PrivacyCfg()
    retention: RetentionCfg = RetentionCfg()
    logging: LoggingCfg = LoggingCfg()
    mesh: MeshCfg = MeshCfg()


class BoardsFile(_Strict):
    boards: list[BoardCfg]

    @field_validator("boards")
    @classmethod
    def _unique(cls, v: list[BoardCfg]) -> list[BoardCfg]:
        names = [b.name for b in v]
        if len(names) != len(set(names)):
            raise ValueError("board names must be unique")
        if not v:
            raise ValueError("at least one board is required")
        return v


class PeersFile(_Strict):
    peers: list[PeerCfg] = []

    @field_validator("peers")
    @classmethod
    def _unique(cls, v: list[PeerCfg]) -> list[PeerCfg]:
        if len({p.id for p in v}) != len(v):
            raise ValueError("peer ids must be unique")
        if len({p.public_key for p in v}) != len(v):
            raise ValueError("two peers have the same public key")
        return v


class Paths(_Strict):
    config_dir: Path
    db_path: Path
    backup_dir: Path
    migrations_dir: Path


class Settings(_Strict):
    bbs: BBSConfig
    boards: list[BoardCfg]
    peers: list[PeerCfg]
    paths: Paths
    service_tokens: dict[str, str]  # service name -> token (never logged)

    def public_dict(self) -> dict:
        """Effective configuration with nothing secret in it (for the admin console)."""
        return {
            "bbs": self.bbs.model_dump(),
            "boards_seed": [b.model_dump() for b in self.boards],
            "peers": [p.model_dump() for p in self.peers],
            "paths": {k: str(v) for k, v in self.paths.model_dump().items()},
            "services_with_tokens": sorted(self.service_tokens),
        }


def _load_yaml(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _read_secret(path_str: str | None, name: str) -> str | None:
    if not path_str:
        return None
    path = Path(path_str)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"{name}: secret file {path} not found") from exc
    except PermissionError as exc:
        raise ConfigError(f"{name}: secret file {path} not readable by this user") from exc
    if len(token) < 32:
        raise ConfigError(f"{name}: secret in {path} is shorter than 32 characters")
    return token


def _fmt(exc: ValidationError, where: str) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{where}: {loc}: {err['msg']}")
    return "\n".join(parts)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    config_dir = Path(env.get("BBS_CONFIG_DIR", "/etc/bbs"))

    raw = _load_yaml(config_dir / "bbs.yaml")
    node = raw.get("node") or {}
    if not isinstance(node, dict):
        raise ConfigError("bbs.yaml: node must be a mapping (id, name, ...)")
    node = dict(node)
    if env.get("BBS_NODE_ID"):
        node["id"] = env["BBS_NODE_ID"]
    if env.get("BBS_NODE_NAME"):
        node["name"] = env["BBS_NODE_NAME"]
    raw["node"] = node
    if env.get("BBS_LOG_LEVEL"):
        logging_cfg = raw.get("logging") or {}
        if not isinstance(logging_cfg, dict):
            raise ConfigError("bbs.yaml: logging must be a mapping")
        raw["logging"] = {**logging_cfg, "level": env["BBS_LOG_LEVEL"].upper()}

    try:
        bbs = BBSConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_fmt(exc, "bbs.yaml")) from exc
    try:
        boards = BoardsFile.model_validate(_load_yaml(config_dir / "boards.yaml")).boards
    except ValidationError as exc:
        raise ConfigError(_fmt(exc, "boards.yaml")) from exc
    try:
        peers = PeersFile.model_validate(_load_yaml(config_dir / "peers.yaml")).peers
    except ValidationError as exc:
        raise ConfigError(_fmt(exc, "peers.yaml")) from exc
    if any(p.id == bbs.node.id for p in peers):
        raise ConfigError("peers.yaml: a peer has this node's own id")
    if bbs.messages.emergency_default_expiry_hours > bbs.messages.emergency_max_expiry_hours:
        raise ConfigError("bbs.yaml: messages.emergency_default_expiry_hours exceeds emergency_max_expiry_hours")

    tokens: dict[str, str] = {}
    for service, var in (("ssh", "BBS_SSH_GATEWAY_TOKEN_FILE"), ("admin", "BBS_ADMIN_GATEWAY_TOKEN_FILE"),
                         ("sync", "BBS_SYNC_GATEWAY_TOKEN_FILE")):
        tok = _read_secret(env.get(var), var)
        if tok:
            tokens[service] = tok
    if len(set(tokens.values())) != len(tokens):
        raise ConfigError("each gateway must have its own token (two token files are identical)")

    paths = Paths(
        config_dir=config_dir,
        db_path=Path(env.get("BBS_DB_PATH", "/data/bbs.sqlite3")),
        backup_dir=Path(env.get("BBS_BACKUP_DIR", "/backups")),
        migrations_dir=Path(env.get("BBS_MIGRATIONS_DIR", "/app/migrations")),
    )
    return Settings(bbs=bbs, boards=boards, peers=peers, paths=paths, service_tokens=tokens)
