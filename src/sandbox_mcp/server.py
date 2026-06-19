"""MCP tool definitions and the combined ASGI app (/mcp + /files)."""

import logging
import shlex
import sys
from urllib.parse import urlparse

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
shell / the sandbox", use THIS server. The same toolset also includes push_file /
pull_file, which move files between the user's PHONE and this sandbox -- use those for
phone<->sandbox transfer (details under "Paths and files").

Running things:
- exec(sandbox, command) runs a shell command and WAITS for it to finish. The sandbox
  is created automatically on first use and PERSISTS across turns -- reuse one short
  name (e.g. "work") for related steps so files stay around. If a result has
  sandbox_created=true, that sandbox did NOT exist (e.g. auto-removed after long
  idle) and was made fresh and EMPTY -- do not assume earlier files survived;
  recreate or re-upload what you need.
- The connection to this server times out after about 30 seconds, so exec is ONLY for
  quick commands that clearly finish well under 30s. For ANYTHING that may take longer
  -- installing/compiling packages, downloading, converting or processing files, any
  heavy Python/data work -- do NOT use exec. Use run_background(sandbox, command), which
  returns a job_id immediately, then poll get_job(job_id) until it is finished. This is
  the ONLY reliable way to run slow commands.
- If a call ever fails with "socket timeout" / "Socket timeout has expired" / a
  timed-out error, it just means the command was too slow for one synchronous request
  (it may even still be running server-side). This is NOT a problem with your approach:
  do NOT rewrite the task or switch language (e.g. C instead of Python) to "go faster".
  Re-run the SAME command with run_background and poll get_job. Note: raising exec's
  `timeout` argument does NOT help here -- that only changes the server-side command
  limit, not the ~30s connection timeout. run_background is the fix.
- When polling get_job: it already waits up to ~15s per call for the job to finish, so
  to keep waiting just call it again at a steady pace -- do NOT fire it many times in a
  rapid burst. Each result carries elapsed_seconds / log_bytes that advance over time, so
  a still-running job looks different each poll (not a stuck loop). Stop polling once
  status is "finished".
- To cancel a running job (e.g. "stop / cancel that task", "停掉/取消那个任务"), call
  stop_job(job_id).

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
- Paths are relative to the sandbox working directory (which is /workspace inside the
  sandbox). Prefer plain names like "input.zip" or "out/result.csv". An absolute
  /workspace/... path also works (it is the same place). Other absolute paths like
  /root/... or /home/... are NOT reachable by upload_url / download_url / read_text --
  put files under the workspace.
- Small text (scripts, configs, short results): write_text / read_text.
- To bring a file FROM THE PHONE into the sandbox, call
  push_file(local_path, sandbox, remote_path) in ONE step: local_path is the absolute
  phone path (e.g. /sdcard/Download/x.zip), sandbox is this sandbox's name, remote_path
  is the destination name in the workspace (e.g. "input.zip"). It transfers the bytes
  directly -- you do NOT need upload_url, and the sandbox CANNOT read phone paths itself
  (never pass /sdcard/... to exec/fetch_url).
  IMPORTANT: remote_path MUST be a RELATIVE name like "input.zip" or "data/in.csv",
  NOT an absolute path like "/tmp/input.zip". The workspace is /workspace inside the
  container -- /tmp, /home, /root are SEPARATE places. If you use /tmp/input.zip the
  file ends up at /workspace/tmp/input.zip, which is NOT the same place. Use just
  "input.zip" and it lands in the working directory where exec can find it.
- To save a sandbox file back to the PHONE, call
  pull_file(sandbox, remote_path, local_path) -- remote_path is the file in the workspace
  (same rule: relative name, NOT /tmp/...), local_path is where to write it on the phone
  (e.g. /sdcard/Download/result.csv).
- fetch_url(sandbox, url, dest) is ONLY for a file already hosted on a PUBLIC http(s)
  URL. Never point it at a URL from this same server -- that loops back and hangs.
- Files OUT to the user as a link: download_url(sandbox, src) returns an HTTPS link you
  can give the user to open in a browser. (To put a file onto the phone's storage, use
  pull_file instead.)
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
    the sandbox). ONLY for quick commands that finish well under 30s -- the client
    connection times out around 30 seconds. For anything slower (installs, compiling,
    downloads, heavy processing) use run_background instead; raising `timeout` will NOT
    avoid the connection timeout. timeout in seconds (0=default, server-side only)."""
    _tlog(f"TOOL exec sandbox={sandbox} cmd={command[:300]!r}")
    return sandboxes.exec_command(sandbox, command, timeout or None)


@mcp.tool(name="run_background")
def run_background_tool(sandbox: str, command: str, timeout: int = 0) -> dict:
    """Start a long-running command in the background and return a job_id
    immediately, avoiding the ~30s client connection timeout. Use this for anything
    slow (installs, compiling, downloads, heavy processing). Poll progress with
    get_job. timeout in seconds (0=default)."""
    _tlog(f"TOOL run_background sandbox={sandbox} cmd={command[:300]!r}")
    return jobs.start(sandbox, command, timeout or None)


@mcp.tool(name="get_job")
def get_job_tool(job_id: str, tail_lines: int = 200) -> dict:
    """Get a background job's status (running/finished), exit code and the last
    log lines. This call already waits up to ~15s for the job to finish before
    returning, so just call it again to keep waiting -- no need to spin rapidly.
    The result includes elapsed_seconds and log_bytes so progress is always visible."""
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
    """Make the sandbox download a PUBLIC http(s):// URL directly into its
    workspace at `dest` (server-side, full bandwidth). Use this only for files
    already hosted on the internet. To bring a file FROM THE PHONE into the
    sandbox, do NOT use this -- call upload_url(sandbox, dest) and then the local
    bridge's push_file(phone_path, that_upload_url)."""
    _tlog(f"TOOL fetch_url sandbox={sandbox} url={url} dest={dest}")
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return {
            "error": (
                f"fetch_url only accepts http(s):// URLs (got scheme "
                f"{u.scheme or 'none'!r}). The sandbox cannot read phone paths like "
                "file:///sdcard/... To bring a PHONE file in, call "
                "upload_url(sandbox, dest) to get an upload URL, then use the local "
                "bridge's push_file(phone_path, upload_url)."
            )
        }
    # Refuse to fetch our own public endpoint: that makes the sandbox call back
    # into this server through the tunnel, blocking the worker on a request that
    # loops to itself -- it wedges the server. A phone file must go via upload_url
    # + the bridge's push_file, never by fetching a /files URL from inside.
    own = urlparse(config.PUBLIC_BASE_URL)
    if u.netloc and own.netloc and u.netloc == own.netloc:
        return {
            "error": (
                "Refusing to fetch this server's own URL from inside the sandbox "
                "(it would loop back and hang). If this is a download_url you just "
                "made, the file is already produced by the sandbox -- give that URL "
                "to the user directly. To bring a PHONE file IN, use "
                "upload_url(sandbox, dest) + the bridge's push_file(phone_path, url)."
            )
        }
    # Bounded timeouts so a slow/stuck URL can never hang a worker indefinitely.
    cmd = (
        f"curl -fsSL --connect-timeout 20 --max-time 300 "
        f"-o {shlex.quote(dest)} {shlex.quote(url)} && echo saved {dest}"
    )
    return sandboxes.exec_command(sandbox, cmd, 320)


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
    # Stable bearer-authed transfer endpoints used by the gateway bridge's
    # self-contained push_file/pull_file (no per-file signed URL through the LLM).
    app.router.routes.append(
        Route("/files/push", files.push, methods=["POST", "PUT"])
    )
    app.router.routes.append(Route("/files/pull", files.pull, methods=["GET"]))
    app.router.routes.append(Route("/healthz", files.health, methods=["GET"]))
    # Patch: Miclaw may omit the required Accept header, causing 406 from the MCP
    # SDK.  Inject it at the ASGI layer before the SDK sees the request.
    return BearerAuth(_AcceptFixMiddleware(app), config.TOKEN, protect_prefix="/mcp")
