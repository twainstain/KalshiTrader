"""Dashboard entrypoint — uvicorn wrapper around `dashboards.kalshi.create_app`.

Usage:
    PYTHONPATH=src python3.11 src/run_dashboard.py
    # Custom DB or port:
    PYTHONPATH=src python3.11 src/run_dashboard.py \
        --database-url sqlite:///data/kalshi.db --port 8000
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


_THIS = Path(__file__).resolve()
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi Phase-1 dashboard.")
    parser.add_argument("--database-url",
                        default=os.environ.get("DATABASE_URL",
                                               "sqlite:///data/kalshi.db"))
    parser.add_argument("--events-dir",
                        default=os.environ.get("KALSHI_EVENTS_DIR", "logs"),
                        help="Directory the scanner writes events_*.jsonl to. "
                             "/kalshi/phases reads from here.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Dev-mode hot reload (watches src/).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Export the url so the factory picks it up when uvicorn runs the module.
    os.environ["KALSHI_DASHBOARD_DB"] = args.database_url

    import uvicorn
    from dashboards.kalshi import create_app

    app = create_app(database_url=args.database_url, events_dir=args.events_dir)
    log = logging.getLogger(__name__)
    if getattr(app.state, "auth_enabled", False):
        log.info("HTTP Basic auth enabled (DASHBOARD_USER / DASHBOARD_PASS)")
    else:
        log.warning(
            "dashboard running without auth — bind to localhost only. "
            "Set DASHBOARD_USER + DASHBOARD_PASS to require Basic auth."
        )
    if getattr(app.state, "allow_write", False):
        log.info("write controls ENABLED (DASHBOARD_ALLOW_WRITE=1)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
