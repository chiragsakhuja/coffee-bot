from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from .agent import CoffeeAgent
from .bot import CoffeeBot
from .config import load_config
from .render import Renderer
from .sandbox import Sandbox
from .store import MenuStore
from .tools import Services


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every Telegram poll

    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    services = Services(
        store=MenuStore(cfg.data_dir),
        renderer=Renderer(cfg.templates_dir, cfg.output_dir),
        sandbox=Sandbox(
            project_root=cfg.project_root,
            read_roots=(cfg.templates_dir, cfg.data_dir),
            write_roots=(cfg.templates_dir,),
        ),
        tz=cfg.timezone,
    )
    agent = CoffeeAgent(AsyncAnthropic(), cfg.model, services)
    CoffeeBot(cfg.telegram_token, cfg.allowed_user_ids, agent, services).run()


if __name__ == "__main__":
    main()
