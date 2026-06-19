"""Background jobs: long-running commands that outlive a single tool call.

The command is launched *inside* the container via `setsid`, writing its output
to /workspace/.smcp/jobs/<id>.log and its exit code to <id>.exit. Because those
files live on the bind-mounted workspace, the server reads job status straight
from the host filesystem -- the job is fully decoupled from the server process
and survives a server restart.
"""

import shlex
import uuid

from .config import config
from . import sandboxes, state
from .util import smart_decode


def _job_host_dir(name: str):
    p = sandboxes.workspace(name) / ".smcp" / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def start(name: str, command: str, timeout: int | None = None) -> dict:
    c, created = sandboxes.ensure(name)
    job_id = uuid.uuid4().hex[:12]
    _job_host_dir(name)  # ensure the dir exists on the host side too
    log = f"/workspace/.smcp/jobs/{job_id}.log"
    exitf = f"/workspace/.smcp/jobs/{job_id}.exit"
    pidf = f"/workspace/.smcp/jobs/{job_id}.pid"
    t = int(timeout or config.JOB_TIMEOUT_DEFAULT)

    inner = f"timeout {t} sh -c {shlex.quote(command)} > {log} 2>&1; echo $? > {exitf}"
    script = (
        "mkdir -p /workspace/.smcp/jobs; "
        f"setsid sh -c {shlex.quote(inner)} >/dev/null 2>&1 & echo $! > {pidf}"
    )
    c.exec_run(["/bin/sh", "-c", script], detach=True, workdir="/workspace")
    state.add_job(job_id, name, command)
    state.touch_sandbox(name)
    res = {"job_id": job_id, "sandbox": name, "status": "running", "sandbox_created": created}
    if created:
        res["note"] = f"sandbox '{name}' was created fresh and EMPTY for this job"
    return res


def status(job_id: str, tail_lines: int = 200) -> dict:
    j = state.get_job(job_id)
    if not j:
        return {"error": "unknown job_id"}
    name = j["sandbox"]
    state.touch_sandbox(name)  # polling keeps the sandbox alive past idle-stop
    base = sandboxes.workspace(name) / ".smcp" / "jobs"
    logf = base / f"{job_id}.log"
    exitf = base / f"{job_id}.exit"

    running = not exitf.exists()
    exit_code = None
    if exitf.exists():
        try:
            exit_code = int(exitf.read_text().strip())
        except Exception:
            exit_code = None
        state.finish_job(job_id, exit_code)

    log_tail = ""
    if logf.exists():
        lines = smart_decode(logf.read_bytes()).splitlines()
        log_tail = "\n".join(lines[-tail_lines:])

    return {
        "job_id": job_id,
        "sandbox": name,
        "command": j["command"],
        "status": "running" if running else "finished",
        "exit_code": exit_code,
        "log_tail": log_tail,
    }


def stop(job_id: str) -> dict:
    j = state.get_job(job_id)
    if not j:
        return {"error": "unknown job_id"}
    name = j["sandbox"]
    pidf = f"/workspace/.smcp/jobs/{job_id}.pid"
    c, _ = sandboxes.ensure(name)
    # Kill the whole process group started by setsid (pid == pgid).
    c.exec_run(
        ["/bin/sh", "-c", f"kill -TERM -$(cat {pidf} 2>/dev/null) 2>/dev/null; true"]
    )
    return {"job_id": job_id, "stopped": True}
