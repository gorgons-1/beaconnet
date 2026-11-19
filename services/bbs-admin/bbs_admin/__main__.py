"""bbs-admin: the private administration SSH console (management network only)."""

from bbs_gateway.client import CoreAPI
from bbs_gateway.sshserver import Gateway, GatewayConfig, run

from .menus import run as menu


def build() -> Gateway:
    cfg = GatewayConfig.from_env("admin", 2223, no_auth_users={})  # every login needs an admin key
    cfg.banner = "Scotland BBS administration - authorised sysops only. Activity is logged.\n"
    cfg.max_per_ip = min(cfg.max_per_ip, 3)
    cfg.max_total = min(cfg.max_total, 8)
    return Gateway(cfg=cfg, api=CoreAPI(cfg.core_url, cfg.token), app=menu)


if __name__ == "__main__":
    run(build)
