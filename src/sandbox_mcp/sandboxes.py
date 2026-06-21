"""Docker-backed sandbox lifecycle: create / ensure / exec / destroy / GC.

A "sandbox" is a long-lived container (`smcp-<name>`) running `sleep infinity`,
with a per-sandbox host directory bind-mounted at /workspace. Commands run via
`docker exec`. Files are exchanged through that host directory (see files.py),
which keeps large bytes out of the MCP/LLM channel.
"""

import re
import shlex
import shutil
import time
from pathlib import Path

import docker

from .config import config
from . import state
from .util import smart_decode

LABEL = "io.sandbox_mcp"
NAME_LABEL = f"{LABEL}.name"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")

_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def _cname(name: str) -> str:
    return f"smcp-{name}"


def workspace(name: str) -> Path:
    """Host path bind-mounted into the sandbox as /workspace (created on demand)."""
    p = config.DATA_DIR / name / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _find(name: str):
    try:
        return client().containers.get(_cname(name))
    except docker.errors.NotFound:
        return None


def exists(name: str) -> bool:
    """True if a container for this sandbox exists (running or stopped)."""
    return _find(name) is not None


def list_sandboxes() -> list[dict]:
    out = []
    for c in client().containers.list(all=True, filters={"label": LABEL}):
        name = c.labels.get(NAME_LABEL, c.name)
        meta = state.get_sandbox(name) or {}
        try:
            image = c.image.tags[0] if c.image.tags else c.image.short_id
        except Exception:
            image = "?"
        out.append(
            {
                "name": name,
                "status": c.status,
                "image": image,
                "created_at": meta.get("created_at"),
                "last_used": meta.get("last_used"),
            }
        )
    return out


def create(name: str, image: str | None = None):
    if not valid_name(name):
        raise ValueError(f"invalid sandbox name: {name!r} (use [A-Za-z0-9_-], <=63 chars)")
    existing = _find(name)
    if existing:
        if existing.status != "running":
            existing.start()
        return existing
    if len(client().containers.list(filters={"label": LABEL})) >= config.MAX_SANDBOXES:
        raise RuntimeError(f"too many running sandboxes (max {config.MAX_SANDBOXES})")
    img = image or config.BASE_IMAGE
    host_ws = workspace(name)
    cont = client().containers.run(
        img,
        command=["sleep", "infinity"],
        name=_cname(name),
        labels={LABEL: "1", NAME_LABEL: name},
        volumes={str(host_ws): {"bind": "/workspace", "mode": "rw"}},
        working_dir="/workspace",
        detach=True,
        tty=False,
        mem_limit=config.MEM_LIMIT,
        nano_cpus=int(config.CPUS * 1_000_000_000),
        pids_limit=config.PIDS_LIMIT,
        network_mode=config.SANDBOX_NETWORK,
        cap_drop=["SYS_ADMIN", "NET_RAW"],
        security_opt=["no-new-privileges"],
    )
    state.add_sandbox(name, img)
    return cont


def ensure(name: str, image: str | None = None):
    """Return (running container, created) for `name`, creating/starting as needed.

    `created` is True when the sandbox did not exist and was made fresh (so callers
    can warn the AI that earlier files are gone, e.g. after idle-GC).
    """
    c = _find(name)
    created = False
    if c is None:
        c = create(name, image)
        created = True
    elif c.status != "running":
        c.start()
    state.touch_sandbox(name)
    return c, created


def destroy(name: str) -> bool:
    """Remove the sandbox container if present. Returns True if one existed."""
    c = _find(name)
    existed = c is not None
    if c:
        c.remove(force=True)
    state.remove_sandbox(name)
    return existed


def exec_command(name: str, command: str, timeout: int | None = None) -> dict:
    """Run a shell command and wait. `timeout` is enforced via coreutils `timeout`."""
    c, created = ensure(name)
    t = int(timeout or config.EXEC_TIMEOUT_DEFAULT)
    wrapped = f"timeout {t} sh -c {shlex.quote(command)}"
    res = c.exec_run(["/bin/sh", "-c", wrapped], demux=True, workdir="/workspace")
    state.touch_sandbox(name)
    out, err = res.output if res.output else (None, None)
    result = {
        "exit_code": res.exit_code,
        "stdout": smart_decode(out or b""),
        "stderr": smart_decode(err or b""),
        "timed_out": res.exit_code == 124,
        "sandbox_created": created,
    }
    if created:
        result["note"] = (
            f"sandbox '{name}' did not exist and was created fresh and EMPTY; "
            "earlier files are gone -- recreate/re-upload anything you relied on."
        )
    return result


def _has_active_job(name: str) -> bool:
    """True if a background job is still running (a .pid file with no .exit yet)."""
    jobs_dir = config.DATA_DIR / name / "workspace" / ".smcp" / "jobs"
    if not jobs_dir.is_dir():
        return False
    for pid in jobs_dir.glob("*.pid"):
        if not pid.with_suffix(".exit").exists():
            return True
    return False


def _prune_job_logs(name: str, now: float) -> None:
    """Delete background-job log/exit/pid files older than the retention window."""
    jobs_dir = config.DATA_DIR / name / "workspace" / ".smcp" / "jobs"
    if not jobs_dir.is_dir():
        return
    for f in jobs_dir.iterdir():
        try:
            if now - f.stat().st_mtime > config.JOB_LOG_RETENTION_SECONDS:
                f.unlink()
        except Exception:
            pass


def gc_once() -> None:
    """Stop idle sandboxes, remove long-unused ones, and reclaim disk. Periodic."""
    now = time.time()
    live = set()
    for c in client().containers.list(all=True, filters={"label": LABEL}):
        name = c.labels.get(NAME_LABEL, c.name)
        live.add(name)
        meta = state.get_sandbox(name) or {}
        idle = now - (meta.get("last_used") or 0)
        try:
            if idle > config.IDLE_REMOVE_SECONDS:
                c.remove(force=True)
                state.remove_sandbox(name)
                shutil.rmtree(config.DATA_DIR / name, ignore_errors=True)
                continue
            # don't stop a sandbox that still has a running background job
            if c.status == "running" and idle > config.IDLE_STOP_SECONDS and not _has_active_job(name):
                c.stop()
            _prune_job_logs(name, now)
        except Exception:
            pass
    # reclaim orphaned workspace dirs (no container, untracked, and stale)
    try:
        if config.DATA_DIR.is_dir():
            for d in config.DATA_DIR.iterdir():
                if not d.is_dir() or d.name in live or state.get_sandbox(d.name):
                    continue
                if now - d.stat().st_mtime > config.IDLE_REMOVE_SECONDS:
                    shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
