"""MCP tool definitions and the combined ASGI app (/mcp + /files)."""

import functools
import inspect
import logging
import shlex
import sys
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
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


def guided(*required: str):
    """Make a tool's failures ACTIONABLE so the model fixes its call and retries
    instead of concluding the tool is unusable. A missing required argument (one of
    `required`) returns a clear, named message + the tool's usage line instead of a
    raw pydantic validation dump; a runtime exception becomes a short "adjust and
    retry" error. Required args are given "" defaults in the signature so the SDK lets
    the call reach here, where we own the message."""

    def deco(fn):
        name = fn.__name__.removesuffix("_tool")
        params = ", ".join(inspect.signature(fn).parameters)

        @functools.wraps(fn)
        def wrapper(**kwargs):
            missing = [r for r in required if kwargs.get(r) in (None, "")]
            if missing:
                return {
                    "error": f"missing required argument(s): {', '.join(missing)}",
                    "usage": f"{name}({params})",
                    "fix": "Add the argument(s) and call again -- this is a fixable "
                    "call, not an unavailable tool.",
                }
            try:
                result = fn(**kwargs)
            except Exception as e:
                return {
                    "error": f"{name} failed: {type(e).__name__}: {e}",
                    "fix": "Adjust the arguments (or retry if it looks transient) "
                    "and call again.",
                }
            # A tool-level error dict (e.g. "unknown job_id", "not found") gets a
            # uniform retry hint so the model fixes the call instead of giving up.
            if isinstance(result, dict) and result.get("error") and "fix" not in result:
                result["fix"] = (
                    f"Check the arguments against {name}'s usage and call it again "
                    "-- this is usually fixable, not a dead end."
                )
            return result

        return wrapper

    return deco


def _reframe(name: str, msg: str) -> str:
    """Turn a framework-level ToolError message into actionable guidance."""
    low = msg.lower()
    if "unknown tool" in low:
        return (
            f"{msg}. Only call tools that appear in tools/list; check the exact name "
            "and try again."
        )
    if "validation error" in low:
        return (
            f"Wrong argument type for {name}: an argument did not match the tool's "
            "schema (e.g. a number or list where a string is expected, or vice versa). "
            f"Check each argument's type, fix it, and call {name} again -- this is a "
            f"fixable call, not an unavailable tool.\nDetails: {msg}"
        )
    return (
        f"{msg}\nThis is usually fixable -- adjust the arguments (or retry if it looks "
        "transient) and call the tool again."
    )


class GuidedFastMCP(FastMCP):
    """Reframe framework-level tool errors (argument-type validation failures, unknown
    tool) into actionable guidance, so a mistyped call reads as 'fixable, retry' rather
    than a raw pydantic dump. (Missing-arg and runtime errors are handled earlier by the
    @guided decorator, which returns a dict and never reaches here.)"""

    async def call_tool(self, name: str, arguments: dict):
        try:
            return await super().call_tool(name, arguments)
        except ToolError as e:
            raise ToolError(_reframe(name, str(e))) from e

INSTRUCTIONS = """\
This is the user's Linux sandbox: a real shell with full command execution. It is the
"Linux" / "shell" / "terminal" / "沙箱" the user refers to. For ANY request to run a
command, run code, process or convert files, install packages, or "use Linux / the
shell / the sandbox", use THIS server.

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
  /root/, /home/, /tmp/ are SEPARATE from the workspace and NOT reachable by
  upload_file / download_file / read_text -- and a dest like "tmp/x" lands at
  /workspace/tmp/x, not /tmp/x. Keep files under the workspace with plain relative names.
- Every file tool (write_text, read_text, list_files, upload_file, download_file) takes
  the `sandbox` name as an argument, exactly like exec -- ALWAYS pass it; a /workspace
  path alone does not identify which sandbox.
- Small text (scripts, configs, short results): write_text / read_text.
- Move whole files in or out of the sandbox with upload_file (bring one in) and
  download_file (get one out) -- see each tool's own description for its exact
  arguments. The sandbox only sees its own workspace; it cannot read the client's local
  filesystem, so never pass a host path to exec / fetch_url.
- fetch_url(sandbox, url, dest) is ONLY for a file already hosted on a PUBLIC http(s)
  URL. Never point it at a URL from this same server -- that loops back and hangs.
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
mcp = GuidedFastMCP(
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
@guided()
def list_sandboxes_tool() -> list:
    """List all sandboxes with their status, image and last-used time."""
    return sandboxes.list_sandboxes()


@mcp.tool(name="create_sandbox")
@guided("sandbox")
def create_sandbox_tool(sandbox: str = "", image: str = "") -> dict:
    """Create a persistent sandbox (Docker container). Usually optional, since
    exec / run_background auto-create on first use. `image` defaults to the base
    image; pass e.g. "python:3.12" for a custom one."""
    c = sandboxes.create(sandbox, image or None)
    return {"name": sandbox, "status": c.status}


@mcp.tool(name="destroy_sandbox")
@guided("sandbox")
def destroy_sandbox_tool(sandbox: str = "", delete_files: bool = False) -> dict:
    """Remove a sandbox container. Workspace files are kept on disk unless
    delete_files=true."""
    existed = sandboxes.destroy(sandbox)
    files_removed = False
    if delete_files:
        import shutil

        d = config.DATA_DIR / sandbox
        files_removed = d.exists()
        shutil.rmtree(d, ignore_errors=True)
    if not existed and not files_removed:
        return {
            "error": f"no sandbox named '{sandbox}' exists; nothing to delete",
            "fix": "Call list_sandboxes() to see the real names (don't guess). If it "
            "was already removed, no action is needed.",
        }
    return {"name": sandbox, "destroyed": True}


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------
@mcp.tool(name="exec")
@guided("sandbox", "command")
def exec_tool(sandbox: str = "", command: str = "", timeout: int = 0) -> dict:
    """Run a shell command in the sandbox and wait for the result (auto-creates
    the sandbox). ONLY for quick commands that finish well under 30s -- the client
    connection times out around 30 seconds. For anything slower (installs, compiling,
    downloads, heavy processing) use run_background instead; raising `timeout` will NOT
    avoid the connection timeout. timeout in seconds (0=default, server-side only)."""
    _tlog(f"TOOL exec sandbox={sandbox} cmd={command[:300]!r}")
    return sandboxes.exec_command(sandbox, command, timeout or None)


@mcp.tool(name="run_background")
@guided("sandbox", "command")
def run_background_tool(sandbox: str = "", command: str = "", timeout: int = 0) -> dict:
    """Start a long-running command in the background and return a job_id
    immediately, avoiding the ~30s client connection timeout. Use this for anything
    slow (installs, compiling, downloads, heavy processing). Poll progress with
    get_job. timeout in seconds (0=default)."""
    _tlog(f"TOOL run_background sandbox={sandbox} cmd={command[:300]!r}")
    return jobs.start(sandbox, command, timeout or None)


@mcp.tool(name="get_job")
@guided("job_id")
def get_job_tool(job_id: str = "", tail_lines: int = 200) -> dict:
    """Get a background job's status (running/finished), exit code and the last
    log lines. This call already waits up to ~15s for the job to finish before
    returning, so just call it again to keep waiting -- no need to spin rapidly.
    The result includes elapsed_seconds and log_bytes so progress is always visible."""
    return jobs.status(job_id, tail_lines)


@mcp.tool(name="stop_job")
@guided("job_id")
def stop_job_tool(job_id: str = "") -> dict:
    """Stop a running background job."""
    return jobs.stop(job_id)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
@mcp.tool(name="list_files")
@guided("sandbox")
def list_files_tool(sandbox: str = "", path: str = ".") -> dict:
    """List files in the sandbox workspace directory."""
    if not sandboxes.exists(sandbox):
        return {
            "error": f"sandbox '{sandbox}' does not exist",
            "fix": "Call list_sandboxes() to see existing sandboxes, or create it "
            "(exec/run_background auto-create on first use), then retry.",
        }
    return files.list_dir(sandbox, path)


@mcp.tool(name="read_text")
@guided("sandbox", "path")
def read_text_tool(sandbox: str = "", path: str = "") -> dict:
    """Read a small text file inline (e.g. a script or log). For large or binary
    files use download_file instead."""
    return files.read_text(sandbox, path)


@mcp.tool(name="write_text")
@guided("sandbox", "path")
def write_text_tool(sandbox: str = "", path: str = "", content: str = "") -> dict:
    """Write/overwrite a small text file, e.g. a script you want to run. For large
    or binary files use upload_file or fetch_url (from a public URL) instead."""
    return files.write_text(sandbox, path, content)


@mcp.tool(name="upload_file")
@guided("sandbox", "dest")
def upload_file_tool(sandbox: str = "", dest: str = "", ttl_seconds: int = 0) -> dict:
    """Bring a file INTO the sandbox workspace at `dest` (a workspace-relative path).
    Returns a one-time HTTPS URL; PUT the bytes to it out-of-band (e.g. `curl -T
    <file> '<url>'`) -- never send big files through tool arguments, they overflow
    the context. Returns a curl example."""
    _tlog(f"TOOL upload_file sandbox={sandbox} dest={dest}")
    url = auth.make_url(sandbox, dest, "put", ttl_seconds or None)
    return {
        "url": url,
        "method": "PUT",
        "dest": dest,
        "example": f"curl -T <local-file> '{url}'",
    }


@mcp.tool(name="download_file")
@guided("sandbox", "src")
def download_file_tool(sandbox: str = "", src: str = "", ttl_seconds: int = 0) -> dict:
    """Get a file OUT of the sandbox workspace (`src`). Returns a one-time HTTPS
    download URL -- give it to the user to open/save. Use this for large or binary
    outputs instead of read_text."""
    _tlog(f"TOOL download_file sandbox={sandbox} src={src}")
    url = auth.make_url(sandbox, src, "get", ttl_seconds or None)
    return {"url": url, "src": src}


@mcp.tool(name="fetch_url")
@guided("sandbox", "url", "dest")
def fetch_url_tool(sandbox: str = "", url: str = "", dest: str = "") -> dict:
    """Make the sandbox download a PUBLIC http(s):// URL directly into its
    workspace at `dest` (server-side, full bandwidth). Use this only for files
    already hosted on the internet. To bring in a LOCAL file (not on a public URL),
    use upload_file instead."""
    _tlog(f"TOOL fetch_url sandbox={sandbox} url={url} dest={dest}")
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return {
            "error": (
                f"fetch_url only accepts http(s):// URLs (got scheme "
                f"{u.scheme or 'none'!r}). The sandbox can only read files inside its "
                "own workspace, not arbitrary host paths (file:///...). To bring a "
                "LOCAL file in, use upload_file."
            )
        }
    # Refuse to fetch our own public endpoint: that makes the sandbox call back
    # into this server through the tunnel, blocking the worker on a request that
    # loops to itself -- it wedges the server. A local file must come in via
    # upload_file, never by fetching a /files URL from inside.
    own = urlparse(config.PUBLIC_BASE_URL)
    if u.netloc and own.netloc and u.netloc == own.netloc:
        return {
            "error": (
                "Refusing to fetch this server's own URL from inside the sandbox "
                "(it would loop back and hang). If this is a download_file link you "
                "just made, the file is already produced by the sandbox -- give that "
                "URL to the user directly. To bring a LOCAL file IN, use upload_file."
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
    # Stable bearer-authed transfer endpoints used by the gateway bridge's by-path
    # upload_file/download_file (no per-file signed URL through the LLM).
    app.router.routes.append(
        Route("/files/push", files.push, methods=["POST", "PUT"])
    )
    app.router.routes.append(Route("/files/pull", files.pull, methods=["GET"]))
    app.router.routes.append(Route("/healthz", files.health, methods=["GET"]))
    # Patch: Miclaw may omit the required Accept header, causing 406 from the MCP
    # SDK.  Inject it at the ASGI layer before the SDK sees the request.
    return BearerAuth(_AcceptFixMiddleware(app), config.TOKEN, protect_prefix="/mcp")
