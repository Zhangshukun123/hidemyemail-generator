from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


SETTING_KEY = "zkgmail_qq_inbox_config_v1"


def active_domain(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (SETTING_KEY,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit("missing-setting")
    return str(json.loads(row[0])["activeDomain"])


if __name__ == "__main__":
    print(active_domain(Path(sys.argv[1])))
