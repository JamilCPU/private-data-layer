"""``pdl-gateway``: load config and secrets, build the app, serve it with uvicorn."""

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from pdl_gateway.app import build_app
from pdl_gateway.backends import build_backends
from pdl_gateway.config import ConfigError, check_secrets, load_config, load_secrets

logger = logging.getLogger("pdl_gateway")

DEFAULT_CONFIG = "/etc/pdl/gateway.yaml"
DEFAULT_SECRETS = "/etc/pdl/secrets.yaml"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pdl-gateway", description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG)),
        help="gateway YAML config (env GATEWAY_CONFIG)",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=Path(os.environ.get("GATEWAY_SECRETS", DEFAULT_SECRETS)),
        help="hashed secrets YAML (env GATEWAY_SECRETS)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the gateway; 1 on a configuration error, 0 when the server stops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
        secrets = load_secrets(args.secrets)
        check_secrets(config, secrets)
        app = build_app(config, secrets, build_backends(config, os.environ))
    except (ConfigError, OSError) as exc:
        sys.stderr.write(f"pdl-gateway: {exc}\n")
        return 1
    logger.info("listening on %s:%d", config.listen_host, config.listen_port)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
