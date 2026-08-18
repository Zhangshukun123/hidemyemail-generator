from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required_token(name: str, value: str) -> str:
    token = str(value or "").strip()
    if len(token) < 32:
        raise RuntimeError(f"{name} 必须至少 32 个字符")
    return token


@dataclass(frozen=True, slots=True)
class ServerSettings:
    shared_db: Path
    service_db: Path
    api_token: str
    code_service_token: str
    code_service_url: str = "http://127.0.0.1:18767"
    internal_base_url: str = "http://127.0.0.1:18769"
    host: str = "127.0.0.1"
    port: int = 18769
    clash_controller: str = "http://127.0.0.1:9099"
    clash_proxy_url: str = "http://127.0.0.1:7899"
    clash_selector: str = ""
    clash_max_latency_ms: int = 1500

    @classmethod
    def from_env(cls) -> ServerSettings:
        host = str(os.environ.get("PROTOCOL_SERVER_HOST") or "127.0.0.1")
        port = int(os.environ.get("PROTOCOL_SERVER_PORT") or 18769)
        if not 1 <= port <= 65535:
            raise RuntimeError("PROTOCOL_SERVER_PORT 无效")
        return cls(
            shared_db=Path(
                os.environ.get("PROTOCOL_SERVER_SHARED_DB")
                or "/shared-data/hidemyemail.db"
            ),
            service_db=Path(
                os.environ.get("PROTOCOL_SERVER_DB")
                or "/data/protocol-registration-server.db"
            ),
            api_token=_required_token(
                "PROTOCOL_SERVER_API_TOKEN",
                os.environ.get("PROTOCOL_SERVER_API_TOKEN", ""),
            ),
            code_service_token=_required_token(
                "PROTOCOL_SERVER_CODE_TOKEN",
                os.environ.get("PROTOCOL_SERVER_CODE_TOKEN", ""),
            ),
            code_service_url=str(
                os.environ.get("PROTOCOL_SERVER_CODE_URL")
                or "http://127.0.0.1:18767"
            ).rstrip("/"),
            internal_base_url=str(
                os.environ.get("PROTOCOL_SERVER_INTERNAL_URL")
                or f"http://127.0.0.1:{port}"
            ).rstrip("/"),
            host=host,
            port=port,
            clash_controller=str(
                os.environ.get("PROTOCOL_SERVER_CLASH_CONTROLLER")
                or "http://127.0.0.1:9099"
            ),
            clash_proxy_url=str(
                os.environ.get("PROTOCOL_SERVER_CLASH_PROXY")
                or "http://127.0.0.1:7899"
            ),
            clash_selector=str(
                os.environ.get("PROTOCOL_SERVER_CLASH_SELECTOR") or ""
            ),
            clash_max_latency_ms=max(
                50,
                min(
                    10000,
                    int(
                        os.environ.get("PROTOCOL_SERVER_CLASH_MAX_LATENCY_MS")
                        or 1500
                    ),
                ),
            ),
        )
