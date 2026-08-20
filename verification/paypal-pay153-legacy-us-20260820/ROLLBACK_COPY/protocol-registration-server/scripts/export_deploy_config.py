from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


ALLOWED_KEYS = (
    "registration_proxy_config_v1",
    "card_link_proxy_config_v1",
    "zkgmail_qq_inbox_config_v1",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a temporary SQLite package containing allowed server settings"
    )
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--output-db", required=True, type=Path)
    args = parser.parse_args()
    source = args.source_db.resolve()
    output = args.output_db.resolve()
    if output.exists():
        raise RuntimeError(f"输出文件已存在：{output}")
    source_uri = f"file:{source.as_posix()}?mode=ro"
    placeholders = ",".join("?" for _ in ALLOWED_KEYS)
    with sqlite3.connect(source_uri, uri=True) as source_db:
        rows = source_db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            ALLOWED_KEYS,
        ).fetchall()
    with sqlite3.connect(output) as output_db:
        output_db.execute(
            "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        output_db.executemany(
            "INSERT INTO settings(key, value) VALUES (?, ?)", rows
        )
    print("CONFIG_KEYS=" + ",".join(sorted(str(row[0]) for row in rows)))


if __name__ == "__main__":
    main()
