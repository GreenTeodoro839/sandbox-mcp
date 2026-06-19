"""Runtime configuration, all via environment variables (see .env.example)."""

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


class Config:
    # --- auth / public address ---
    # Bearer token the MCP client (Miclaw) must send on /mcp requests.
    TOKEN: str = os.environ.get("SMCP_TOKEN", "")
    # Key used to HMAC-sign file upload/download URLs. Defaults to TOKEN.
    SIGNING_KEY: bytes = os.environ.get("SMCP_SIGNING_KEY", TOKEN).encode()
    # Public base URL as seen through the frp tunnel, e.g. https://sandbox.example.com
    PUBLIC_BASE_URL: str = os.environ.get(
        "SMCP_PUBLIC_BASE_URL", "http://127.0.0.1:8000"
    ).rstrip("/")

    # --- where the server listens (frpc forwards to this) ---
    HOST: str = os.environ.get("SMCP_HOST", "127.0.0.1")
    PORT: int = _int("SMCP_PORT", 8000)

    # --- storage ---
    # Host directory holding every sandbox's bind-mounted /workspace.
    DATA_DIR: Path = Path(os.environ.get("SMCP_DATA_DIR", "/var/lib/sandbox-mcp/data"))
    STATE_DB: Path = Path(
        os.environ.get("SMCP_STATE_DB", "/var/lib/sandbox-mcp/state.db")
    )

    # --- sandbox containers ---
    BASE_IMAGE: str = os.environ.get("SMCP_BASE_IMAGE", "sandbox-mcp-base:latest")
    SANDBOX_NETWORK: str = os.environ.get("SMCP_SANDBOX_NETWORK", "bridge")  # "none" disables net
    MEM_LIMIT: str = os.environ.get("SMCP_MEM_LIMIT", "2g")
    CPUS: float = _float("SMCP_CPUS", 2.0)
    PIDS_LIMIT: int = _int("SMCP_PIDS_LIMIT", 512)
    MAX_SANDBOXES: int = _int("SMCP_MAX_SANDBOXES", 20)

    # --- lifecycle / timeouts (seconds) ---
    IDLE_STOP_SECONDS: int = _int("SMCP_IDLE_STOP_SECONDS", 2 * 3600)
    IDLE_REMOVE_SECONDS: int = _int("SMCP_IDLE_REMOVE_SECONDS", 7 * 24 * 3600)
    GC_INTERVAL_SECONDS: int = _int("SMCP_GC_INTERVAL_SECONDS", 300)
    EXEC_TIMEOUT_DEFAULT: int = _int("SMCP_EXEC_TIMEOUT", 60)
    JOB_TIMEOUT_DEFAULT: int = _int("SMCP_JOB_TIMEOUT", 3600)
    JOB_LOG_RETENTION_SECONDS: int = _int("SMCP_JOB_LOG_RETENTION", 86400)
    URL_TTL: int = _int("SMCP_URL_TTL", 3600)

    # --- inline file read guard ---
    READ_TEXT_MAX_BYTES: int = _int("SMCP_READ_TEXT_MAX_BYTES", 200_000)


config = Config()
