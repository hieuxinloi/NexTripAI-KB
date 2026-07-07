from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from .router import router


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def create_app() -> FastAPI:
    load_dotenv_if_available()
    app = FastAPI(title="NexTripAI KB")
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    uvicorn.run("nextrip_graphrag.api.app:app", host="0.0.0.0", port=8010, reload=True)


if __name__ == "__main__":
    main()
