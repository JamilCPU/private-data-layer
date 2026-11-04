"""``pdl-assistant``: load config and secrets, wire the agent and the web app, serve it."""

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette

from .agent import Agent
from .auth import GatewayLogin, SessionSigner
from .config import AssistantConfig, AssistantSecrets, ConfigError, load_config, load_secrets
from .gateway_client import GatewayClient
from .llm import OllamaClient
from .memory import MemoryStore
from .web import build_app

logger = logging.getLogger("pdl_assistant")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pdl-assistant", description=__doc__)
    config = os.environ.get("ASSISTANT_CONFIG")
    secrets = os.environ.get("ASSISTANT_SECRETS")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(config) if config else None,
        help="assistant YAML config (env ASSISTANT_CONFIG; omit for env-only config)",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=Path(secrets) if secrets else None,
        help="assistant secrets YAML (env ASSISTANT_SECRETS; default: secrets_file from config)",
    )
    return parser.parse_args(argv)


def _read_client_secret(path: str) -> str:
    secret = Path(path).read_text(encoding="utf-8").strip()
    if not secret:
        raise ConfigError(f"{path}: client secret file is empty")
    return secret


def _assemble(config: AssistantConfig, secrets: AssistantSecrets, client_secret: str) -> Starlette:
    """Build the store, clients, agent and app; the app's shutdown closes them all."""
    memory = MemoryStore(config.db_path)
    gateway = GatewayClient(
        config.gateway_url, config.client_id, client_secret, tool_timeout_s=config.tool_timeout_s
    )
    llm = OllamaClient(config.ollama_url, config.model, timeout_s=config.model_timeout_s)
    login = GatewayLogin(config.gateway_url, config.public_url, memory)
    agent = Agent(
        llm,
        gateway,
        memory,
        max_tool_steps=config.max_tool_steps,
        system_prompt_extra=config.system_prompt_extra,
    )

    async def close() -> None:
        for closer in (login.aclose, gateway.aclose, llm.aclose):
            await closer()
        memory.close()

    return build_app(
        config,
        secrets,
        agent,
        login,
        memory,
        signer=SessionSigner(secrets.cookie_secret, config.session_ttl_s),
        on_shutdown=close,
    )


def _load(args: argparse.Namespace) -> tuple[AssistantConfig, Starlette]:
    config = load_config(args.config, os.environ)
    secrets = load_secrets(args.secrets or Path(config.secrets_file))
    return config, _assemble(config, secrets, _read_client_secret(config.client_secret_file))


def main(argv: list[str] | None = None) -> int:
    """Run the assistant; 1 on a configuration error, 0 when the server stops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    try:
        config, app = _load(args)
    except (ConfigError, OSError, sqlite3.Error) as exc:
        sys.stderr.write(f"pdl-assistant: {exc}\n")
        return 1
    logger.info("listening on %s:%d", config.listen_host, config.listen_port)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
