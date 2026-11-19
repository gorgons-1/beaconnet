"""bbs-ssh: the public SSH terminal gateway."""

from bbs_gateway.client import CoreAPI
from bbs_gateway.sshserver import Gateway, GatewayConfig, run

from .menus import register_only, run as menu

# Usernames that need no credentials. Everything else must present a registered key.
NO_AUTH_USERS = {"guest": "guest", "bbs": "guest", "new": "register", "register": "register"}


def build() -> Gateway:
    cfg = GatewayConfig.from_env("ssh", 2222, NO_AUTH_USERS)
    cfg.banner = (
        "BeaconNet\n"
        f"Visitors: ssh -p {cfg.public_port} guest@{cfg.public_host}   "
        f"New account: ssh -p {cfg.public_port} new@{cfg.public_host}\n"
    )
    return Gateway(cfg=cfg, api=CoreAPI(cfg.core_url, cfg.token), app=menu, register_app=register_only,
                   client_api=True)


if __name__ == "__main__":
    run(build)
