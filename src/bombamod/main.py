"""BombaMod executable entry point."""

from __future__ import annotations

import logging
import sys

from bombamod.bot import BombaModBot, install_commands
from bombamod.config import ConfigurationError, Settings
from bombamod.storage import Store


def main() -> None:
    try:
        settings = Settings.from_env()
        database_url = settings.sqlalchemy_database_url()
    except ConfigurationError as exc:
        print(f"BombaMod configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.INFO)
    store = Store(database_url)
    bot = BombaModBot(settings, store)
    install_commands(bot)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("BombaMod stopped")
