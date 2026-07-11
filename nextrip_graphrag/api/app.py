from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from ..config import Settings
from ..logging import configure_logging, install_request_logging
from .router import router


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def create_app() -> FastAPI:
    load_dotenv_if_available()
    settings = Settings.from_env()
    configure_logging(service="nextrip-kb", level=settings.log_level)
    app = FastAPI(title="NexTripAI KB")
    install_request_logging(app)
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    uvicorn.run("nextrip_graphrag.api.app:app", host="0.0.0.0", port=8010, reload=True)


if __name__ == "__main__":
    main()
