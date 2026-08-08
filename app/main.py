import logging
from pathlib import Path

from fastapi import FastAPI

from .config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_DB_URL = "data/interview.db"


def build_app(config_path: Path = Path("config.yaml")) -> FastAPI:
    config = load_config(config_path)
    Path(DEFAULT_DB_URL).parent.mkdir(exist_ok=True)
    from .web.routes import create_app

    return create_app(config, enable_scheduler=True)


app = build_app()
