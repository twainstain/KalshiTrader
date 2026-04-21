"""Cron entrypoint: re-aggregate JSONL phase_timing events into SQL.

Run every minute via cron / systemd timer:

    * * * * *  cd /srv/kalshi && PYTHONPATH=src python3.11 \
        scripts/rollup_phase_timings.py --database-url sqlite:///data/kalshi.db

The script is idempotent — it re-aggregates the trailing `--lookback-minutes`
window on every run. Crashes or missed runs self-heal next time.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


_THIS = Path(__file__).resolve()
_SRC = _THIS.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-dir", default="logs",
                        help="Directory with events_YYYY-MM-DD.jsonl files.")
    parser.add_argument("--database-url",
                        default=os.environ.get(
                            "DATABASE_URL", "sqlite:///data/kalshi.db",
                        ),
                        help="DB URL to upsert rollups into. sqlite:// only.")
    parser.add_argument("--bucket-seconds", type=int, default=60,
                        help="Bucket size in seconds. Default: 60 (1 minute).")
    parser.add_argument("--lookback-minutes", type=int, default=120,
                        help="Re-aggregate this many trailing minutes each "
                             "run. Larger = more tolerant of gaps, slower. "
                             "Default: 120.")
    parser.add_argument("--retain-days", type=int, default=None,
                        help="If set, delete rows with bucket_ts older than "
                             "this many days. Omit to keep forever.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("rollup_phase_timings")

    import phase_timing_rollup as ptr
    result = ptr.run(
        events_dir=args.events_dir,
        database_url=args.database_url,
        bucket_seconds=args.bucket_seconds,
        lookback_minutes=args.lookback_minutes,
        retain_days=args.retain_days,
    )
    log.info(
        "rollup done: %d rows upserted (lookback=%dm, pruned=%d)",
        result["rows_written"], args.lookback_minutes, result["rows_pruned"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
