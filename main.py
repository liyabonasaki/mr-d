"""
Entry point.

    python main.py
    # or
    uvicorn main:app --reload
"""

import logging

import uvicorn

from app.api import app  # noqa: F401 – re-exported so uvicorn can find it

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
