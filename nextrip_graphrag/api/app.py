from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ..config import Settings
from ..logging import configure_logging, install_request_logging
from .dependencies import KbServices
from .router import router


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    services = KbServices(Settings.from_env())
    app.state.kb_services = services
    try:
        yield
    finally:
        services.close()


def create_app() -> FastAPI:
    load_dotenv_if_available()
    settings = Settings.from_env()
    configure_logging(service="nextrip-kb", level=settings.log_level)
    app = FastAPI(title="NexTripAI KB", lifespan=lifespan)
    install_request_logging(app)
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    uvicorn.run("nextrip_graphrag.api.app:app", host="0.0.0.0", port=8010, reload=True)


if __name__ == "__main__":
    main()
