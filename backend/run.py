"""Convenience launcher: `python run.py` starts the local server."""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
