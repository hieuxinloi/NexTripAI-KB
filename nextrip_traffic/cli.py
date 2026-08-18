from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "nextrip_traffic.api:app",
        host="127.0.0.1",
        port=8010,
    )


if __name__ == "__main__":
    main()