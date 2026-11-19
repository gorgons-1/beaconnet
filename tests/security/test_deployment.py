"""Static checks on the deployment and the code base (acceptance criteria 'Security' / 'Database')."""

import re
from pathlib import Path

import yaml

from conftest import ROOT

COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]


def test_expected_services():
    assert set(SERVICES) == {"bbs-core", "bbs-ssh", "bbs-admin", "bbs-sync"}


def test_no_privileged_and_hardened():
    for name, svc in SERVICES.items():
        assert svc.get("privileged") is False, name
        assert svc["read_only"] is True, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "cap_add" not in svc, name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["user"] == "10001:10001", name
        assert svc.get("network_mode") != "host" and svc.get("pid") != "host" and svc.get("ipc") != "host", name
        assert "devices" not in svc, name
        for key in ("mem_limit", "cpus", "pids_limit", "healthcheck", "restart"):
            assert key in svc, (name, key)


def test_no_docker_socket_or_host_root():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "docker.sock" not in text
    for name, svc in SERVICES.items():
        for vol in svc.get("volumes", []):
            src = vol.split(":")[0]
            assert src not in ("/", "/var/run", "/run", "/etc", "/var/lib/docker"), (name, vol)


def test_only_core_has_database():
    assert any(v.startswith("bbs-db:") for v in SERVICES["bbs-core"]["volumes"])
    for name in ("bbs-ssh", "bbs-admin", "bbs-sync"):
        assert not any("bbs-db" in v for v in SERVICES[name].get("volumes", [])), name
        assert "volumes" not in SERVICES[name], name


def test_core_not_published_and_internal_only():
    core = SERVICES["bbs-core"]
    assert "ports" not in core and "expose" not in core
    for net in core["networks"]:
        assert COMPOSE["networks"][net].get("internal") is True, net


def test_admin_separate_and_private():
    assert set(SERVICES["bbs-admin"]["networks"]).isdisjoint(SERVICES["bbs-ssh"]["networks"])
    port = SERVICES["bbs-admin"]["ports"][0]
    assert port.startswith("${BBS_ADMIN_BIND:-127.0.0.1}:")
    env = (ROOT / ".env.example").read_text()
    assert re.search(r"^BBS_ADMIN_BIND=127\.0\.0\.1\b", env, re.M)


def test_gateways_get_only_their_own_secrets():
    assert set(SERVICES["bbs-ssh"]["secrets"]) == {"ssh_gateway_token", "ssh_host_ed25519_key", "ssh_host_rsa_key"}
    assert set(SERVICES["bbs-admin"]["secrets"]) == {"admin_gateway_token", "admin_host_ed25519_key"}
    assert set(SERVICES["bbs-sync"]["secrets"]) == {"sync_gateway_token", "node_ed25519_key"}
    assert set(SERVICES["bbs-core"]["secrets"]) == {"ssh_gateway_token", "admin_gateway_token", "sync_gateway_token"}


def test_sync_isolated_from_user_and_admin_networks():
    sync = set(SERVICES["bbs-sync"]["networks"])
    for other in ("bbs-ssh", "bbs-admin"):
        assert sync.isdisjoint(SERVICES[other]["networks"]), other
    assert COMPOSE["networks"]["core-sync"]["internal"] is True
    assert "core-sync" in SERVICES["bbs-core"]["networks"]
    # core's host-state mount is read-only
    assert "./state:/etc/bbs-state:ro" in SERVICES["bbs-core"]["volumes"]
    assert "./config:/etc/bbs:ro" in SERVICES["bbs-core"]["volumes"]


def test_only_core_code_opens_sqlite():
    for d in ("shared", "services/bbs-ssh", "services/bbs-admin", "services/bbs-sync"):
        for f in (ROOT / d).rglob("*.py"):
            assert "sqlite3" not in f.read_text(), f


SOURCE_DIRS = ("services", "shared", "scripts", "config.example", "migrations", "ansible", "docs")


def _source_files():
    for d in SOURCE_DIRS:
        for f in (ROOT / d).rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc",):
                yield f


def test_no_shell_execution_in_services():
    banned = re.compile(r"\b(subprocess|os\.system|os\.popen|pty\.spawn|eval\(|exec\(|pickle|shell=True|create_subprocess)")
    for f in (ROOT / "services").rglob("*.py"):
        assert not banned.search(f.read_text()), f
    for f in (ROOT / "shared").rglob("*.py"):
        assert not banned.search(f.read_text()), f


def test_no_hard_coded_secrets():
    patterns = [
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"(?i)(password|passwd|secret|token|api_key)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ]
    for f in _source_files():
        text = f.read_text(errors="ignore")
        for p in patterns:
            assert not p.search(text), (f, p.pattern)
    secrets_dir = ROOT / "secrets"
    tracked = {p.name for p in secrets_dir.iterdir()} - {"README.md"}
    ignore = (ROOT / ".gitignore").read_text()
    assert "secrets/*" in ignore and ".env" in ignore
    dockerignore = (ROOT / ".dockerignore").read_text()
    assert "secrets" in dockerignore and ".env" in dockerignore  # never baked into images
    del tracked


def test_env_example_has_no_secrets():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0]
            assert not re.search(r"(?i)token|secret|password|key$", key), key


def test_dependencies_pinned():
    for svc in ("bbs-core", "bbs-ssh", "bbs-admin", "bbs-sync"):
        for line in (ROOT / f"services/{svc}/requirements.lock").read_text().splitlines():
            if line and not line.startswith("#"):
                assert re.match(r"^[A-Za-z0-9_.\-]+==[0-9][^\s]*$", line), (svc, line)
        df = (ROOT / f"services/{svc}/Dockerfile").read_text()
        assert re.search(r"^FROM python:\d+\.\d+\.\d+-slim-\w+$", df, re.M), svc
        assert "USER 10001:10001" in df, svc


def test_migrations_numbered():
    files = sorted(p.name for p in (ROOT / "migrations").glob("*.sql"))
    assert files and all(re.match(r"^\d{4}_[a-z0-9_]+\.sql$", f) for f in files)


def test_logs_never_include_bodies_or_keys():
    """Log calls in core pass only metadata - no body, public_key, token or contact fields."""
    for f in list((ROOT / "services").rglob("*.py")) + list((ROOT / "shared").rglob("*.py")):
        for m in re.finditer(r"(?:event|log_event)\(([^)]*)\)", f.read_text()):
            args = m.group(1)
            for bad in ("body=", "public_key=", "token=", "contact=", "password="):
                assert bad not in args, (f, args)


def test_scripts_are_strict():
    for f in (ROOT / "scripts").glob("*.sh"):
        head = f.read_text().splitlines()[:5]
        assert head[0].startswith("#!/usr/bin/env bash") or head[0].startswith("#!/bin/bash"), f
        assert any("set -euo pipefail" in ln for ln in f.read_text().splitlines()[:20]), f


def test_docs_present():
    for doc in ("architecture", "installation", "administration", "security", "disaster-recovery", "radio-integration",
                "mesh", "updating"):
        assert (ROOT / f"docs/{doc}.md").exists(), doc
    assert Path(ROOT / "README.md").exists()


def test_nothing_personal_or_site_specific_in_published_files():
    """The repository is public: no private addresses, hostnames or user names of a real site."""
    allowed = {"127.0.0.1", "0.0.0.0", "1.1.1.1", "10.0.0.0", "172.16.0.0", "192.168.0.0"}  # RFC1918 range names
    ip = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b")
    for f in list(_source_files()) + [ROOT / "README.md", ROOT / ".env.example"]:
        if f.name == "inventory.ini" or "tests" in f.parts:
            continue
        text = f.read_text(errors="ignore")
        hits = [m.group(0) for m in ip.finditer(text) if m.group(0) not in allowed and m.group(0).count(".") == 3]
        assert not hits, (f, hits)
