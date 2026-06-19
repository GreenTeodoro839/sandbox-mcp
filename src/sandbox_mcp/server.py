"""MCP tool definitions and the combined ASGI app (/mcp + /files)."""

import logging
import shlex
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from .config import config
from .auth import BearerAuth
from . import sandboxes, jobs, files, auth

log = logging.getLogger("sandbox_mcp")


def _tlog(msg: str) -> None:
    """Write directly to stderr so it always lands in journald,
    even if the MCP SDK overrides the logging configuration."""
    sys.stderr.write(f"[sandbox-mcp] {msg}\n")
    sys.stderr.flush()

INSTRUCTIONS = """\
This is the user's Linux sandbox: a real shell with full command execution. It is the
"Linux" / "shell" / "terminal" / "沙箱" the user refers to. For ANY request to run a
command, run code, process or convert files, install packages, or "use Linux / the
shell / the sandbox", use THIS server. A separate local file-transfer server may also
be connected; that one ONLY moves files and CANNOT run commands, so never use it for
shell/command tasks.

Running things:
- exec(sandbox, command) runs a shell command and waits. The sandbox is created
  automatically on first use and PERSISTS across turns -- reuse one short name (e.g.
  "work") for related steps so files stay around. If a result has
  sandbox_created=true, that sandbox did NOT exist (e.g. auto-removed after long
  idle) and was made fresh and EMPTY -- do not assume earlier files survived;
  recreate or re-upload what you need.
- For anything longer than a few seconds (installing packages, processing many files),
  use run_background(...) and poll get_job(...); plain exec will time out. To cancel a
  running job (e.g. "stop / cancel that task", "停掉/取消那个任务"), call stop_job(job_id).

Managing sandboxes:
- To see what sandboxes exist (e.g. "list / show / which sandboxes", "查一下有哪些沙箱"),
  call list_sandboxes() -- it returns every sandbox with status and last-used time. Do
  NOT use list_files for this (that lists files INSIDE one sandbox), and never guess
  sandbox names from memory.
- To delete a sandbox (e.g. "remove / delete / 删除沙箱"), call destroy_sandbox(sandbox);
  pass delete_files=true to also wipe its files. To delete several, first list_sandboxes()
  to get the real names, then call destroy_sandbox once per name.
- create_sandbox is optional (exec/run_background auto-create); use it only to pre-make one.

Paths and files (IMPORTANT):
- Every path is RELATIVE to the sandbox working directory. Use plain names like
  "input.zip" or "out/result.csv". Do NOT pass absolute paths like /home/... or
  /root/... to upload_url / download_url / read_text -- they are rejected.
- Small text (scripts, configs, short results): write_text / read_text. Large or binary
  files IN: fetch_url(sandbox,url,dest) if already at a URL, else upload_url(sandbox,dest).
  Files OUT to the user: download_url(sandbox,src).
- Preinstalled: python3 (pypdf, pdfplumber, pandas, requests), git, curl, unzip.
- Chinese/Unicode filenames inside ZIP archives are usually GBK-encoded; plain `unzip`
  will garble them. Extract with Python instead, decoding cp437->gb18030:
      python3 - <<'PY'
      import zipfile
      z = zipfile.ZipFile("archive.zip")
      for i in z.infolist():
          try: i.filename = i.filename.encode("cp437").decode("gb18030")
          except Exception: pass
          z.extract(i)
      PY

Report results concisely; do not dump large file contents into the chat."""

# The MCP transport's DNS-rebinding protection checks the Host header, which frp's
# https2http plugin rewrites -- disable it; access is already gated by the bearer token.
mcp = FastMCP(
    "sandbox-mcp",
    instructions=INSTRUCTIONS,
    # Stateless: no server-side MCP sessions, so a server restart or a flaky client
    # (Miclaw) can't get stuck on "session not found" 404s. Each request is
    # self-contained. json_response returns one JSON body (not an SSE stream).
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# --------------------------------------------------------------------------
# Sandbox management
# --------------------------------------------------------------------------
@mcp.tool(name="list_sandboxes")
def list_sandboxes_tool() -> list:
    """List all sandboxes with their status, image and last-used time."""
    return sandboxes.list_sandboxes()


@mcp.tool(name="create_sandbox")
def create_sandbox_tool(sandbox: str, image: str = "") -> dict:
    """Create a persistent sandbox (Docker container). Usually optional, since
    exec / run_background auto-create on first use. `image` defaults to the base
    image; pass e.g. "python:3.12" for a custom one."""
    c = sandboxes.create(sandbox, image or None)
    return {"name": sandbox, "status": c.status}


@mcp.tool(name="destroy_sandbox")
def destroy_sandbox_tool(sandbox: str, delete_files: bool = False) -> dict:
    """Remove a sandbox container. Workspace files are kept on disk unless
    delete_files=true."""
    sandboxes.destroy(sandbox)
    if delete_files:
        import shutil

        shutil.rmtree(config.DATA_DIR / sandbox, ignore_errors=True)
    return {"name": sandbox, "destroyed": True}


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------
@mcp.tool(name="exec")
def exec_tool(sandbox: str, command: str, timeout: int = 0) -> dict:
    """Run a shell command in the sandbox and wait for the result (auto-creates
    the sandbox). Use this for quick commands. For anything that may run for more
    than a few seconds, use run_background instead. timeout in seconds (0=default)."""
    _tlog(f"TOOL exec sandbox={sandbox} cmd={command[:300]!r}")
    return sandboxes.exec_command(sandbox, command, timeout or None)


@mcp.tool(name="run_background")
def run_background_tool(sandbox: str, command: str, timeout: int = 0) -> dict:
    """Start a long-running command in the background and return a job_id
    immediately. Poll progress with get_job. timeout in seconds (0=default)."""
    _tlog(f"TOOL run_background sandbox={sandbox} cmd={command[:300]!r}")
    return jobs.start(sandbox, command, timeout or None)


@mcp.tool(name="get_job")
def get_job_tool(job_id: str, tail_lines: int = 200) -> dict:
    """Get a background job's status (running/finished), exit code and the last
    log lines."""
    return jobs.status(job_id, tail_lines)


@mcp.tool(name="stop_job")
def stop_job_tool(job_id: str) -> dict:
    """Stop a running background job."""
    return jobs.stop(job_id)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
@mcp.tool(name="list_files")
def list_files_tool(sandbox: str, path: str = ".") -> dict:
    """List files in the sandbox workspace directory."""
    return files.list_dir(sandbox, path)


@mcp.tool(name="read_text")
def read_text_tool(sandbox: str, path: str) -> dict:
    """Read a small text file inline (e.g. a script or log). For large or binary
    files use download_url instead."""
    return files.read_text(sandbox, path)


@mcp.tool(name="write_text")
def write_text_tool(sandbox: str, path: str, content: str) -> dict:
    """Write/overwrite a small text file, e.g. a script you want to run. For
    large or binary files use upload_url instead."""
    return files.write_text(sandbox, path, content)


@mcp.tool(name="upload_url")
def upload_url_tool(sandbox: str, dest: str, ttl_seconds: int = 0) -> dict:
    """Get a one-time HTTPS URL to UPLOAD a (possibly large) file into the
    sandbox workspace at `dest`. The file bytes are PUT to this URL over plain
    HTTP -- never send big files through tool arguments. Returns a curl example."""
    _tlog(f"TOOL upload_url sandbox={sandbox} dest={dest}")
    url = auth.make_url(sandbox, dest, "put", ttl_seconds or None)
    return {
        "upload_url": url,
        "method": "PUT",
        "dest": dest,
        "example": f"curl -T <local-file> '{url}'",
    }


@mcp.tool(name="download_url")
def download_url_tool(sandbox: str, src: str, ttl_seconds: int = 0) -> dict:
    """Get a one-time HTTPS URL to DOWNLOAD a file from the sandbox workspace.
    Give this link to the user to open/save. Use this for large or binary outputs
    instead of read_text."""
    _tlog(f"TOOL download_url sandbox={sandbox} src={src}")
    url = auth.make_url(sandbox, src, "get", ttl_seconds or None)
    return {"download_url": url, "src": src}


@mcp.tool(name="fetch_url")
def fetch_url_tool(sandbox: str, url: str, dest: str) -> dict:
    """Make the sandbox download a remote URL directly into its workspace at
    `dest` (server-side, full bandwidth). Handy when the input file is already
    reachable by a URL, avoiding a round-trip through the phone."""
    _tlog(f"TOOL fetch_url sandbox={sandbox} url={url} dest={dest}")
    cmd = f"curl -fsSL -o {shlex.quote(dest)} {shlex.quote(url)} && echo saved {dest}"
    return sandboxes.exec_command(sandbox, cmd, 600)


# --------------------------------------------------------------------------
# App assembly
# --------------------------------------------------------------------------
class _AcceptFixMiddleware:
    """Pure-ASGI wrapper that ensures POST requests to /mcp carry the
    Accept headers the MCP SDK requires (text/event-stream + application/json).
    Without this, Miclaw gets 406 Not Acceptable on every new session."""

    REQUIRED = "text/event-stream, application/json"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/mcp":
            old = scope.get("headers") or []
            new = []
            found = False
            for name, val in old:
                if name == b"accept":
                    found = True
                    if b"text/event-stream" not in val:
                        val = self.REQUIRED.encode()
                new.append((name, val))
            if not found:
                new.append((b"accept", self.REQUIRED.encode()))
            scope = {**scope, "headers": new}
        await self.app(scope, receive, send)


def build_app():
    """Starlette app with MCP at /mcp and the file side-channel at /files,
    wrapped in bearer-token auth for /mcp."""
    app = mcp.streamable_http_app()  # serves MCP at /mcp, includes its lifespan
    app.router.routes.append(
        Route("/files/get/{sig}", files.download, methods=["GET"])
    )
    app.router.routes.append(
        Route("/files/put/{sig}", files.upload, methods=["PUT", "POST"])
    )
    app.router.routes.append(Route("/healthz", files.health, methods=["GET"]))
    # Patch: Miclaw may omit the required Accept header, causing 406 from the MCP
    # SDK.  Inject it at the ASGI layer before the SDK sees the request.
    return BearerAuth(_AcceptFixMiddleware(app), config.TOKEN, protect_prefix="/mcp")
