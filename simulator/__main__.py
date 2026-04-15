"""Run the FoxESS simulator as a standalone server.

python -m simulator --port 8787
"""

from __future__ import annotations

import argparse
import logging

from aiohttp import web

from .server import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="FoxESS inverter simulator")
    parser.add_argument("--port", type=int, default=8787, help="Server port")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    args = parser.parse_args()

    app = create_app()
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
