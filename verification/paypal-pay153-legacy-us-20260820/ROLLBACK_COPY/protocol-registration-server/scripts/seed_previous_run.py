from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from protocol_registration_server.model import RegistrationRunRepository


TASK_SUMMARY_FIELDS = (
    "id",
    "status",
    "phase",
    "total",
    "completed",
    "succeeded",
    "failed",
    "accounts",
)


def seed_previous_run(snapshot_file: Path, service_db: Path) -> str:
    payload = json.loads(Path(snapshot_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("service"), dict):
        raise ValueError("部署前状态文件缺少 service")
    service: dict[str, Any] = dict(payload["service"])
    task = payload.get("task")
    task = task if isinstance(task, dict) else {}
    service.setdefault("concurrency", int(task.get("concurrency") or 1))
    service.setdefault("useRegistrationKookeey", False)
    service.setdefault("registrationCountry", "JP")
    service.setdefault("verificationCompleted", 0)
    service.setdefault("verificationVerified", 0)
    service.setdefault("verificationResults", [])
    service["taskSummary"] = {
        key: task.get(key) for key in TASK_SUMMARY_FIELDS if key in task
    }
    RegistrationRunRepository(Path(service_db)).save(service)
    return str(service.get("id") or "")


def main() -> None:
    parser = argparse.ArgumentParser(description="保存部署前的服务器注册任务状态")
    parser.add_argument("snapshot_file", type=Path)
    parser.add_argument("service_db", type=Path)
    args = parser.parse_args()
    run_id = seed_previous_run(args.snapshot_file, args.service_db)
    print(f"SEEDED_RUN_ID={run_id}")


if __name__ == "__main__":
    main()
