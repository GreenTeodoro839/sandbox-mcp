"""File side-channel: inline text ops + HMAC-signed HTTP upload/download.

Large/binary files never pass through MCP tool arguments. Instead a tool returns
a signed URL and the bytes travel over plain HTTP (through the frp tunnel),
read/written directly on the sandbox's bind-mounted host workspace.
"""

import logging
from pathlib import Path, PurePosixPath

from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

from .config import config
from . import auth
from .util import smart_decode

log = logging.getLogger("sandbox_mcp")


class PathError(Exception):
    """Raised when a user-supplied path is outside the sandbox workspace."""
    pass


def _safe_path(sandbox: str, rel: str) -> Path:
    """Resolve `rel` under the sandbox workspace, rejecting path traversal.

    Models routinely pass the in-container absolute path -- the workspace is
    bind-mounted at /workspace inside the sandbox, so `exec`/`ls` show files as
    /workspace/foo.  Normalize any /workspace/... absolute path back to a path
    relative to the workspace root before resolving.  Other absolute paths (/tmp,
    /home, /root, ...) are REJECTED with a clear error, because the upload endpoint
    can only write to the workspace -- writing to /workspace/tmp when the model
    asked for /tmp means exec in /tmp won't find the file.

    Returns the resolved Path on success.  Raises PathError on bad paths."""
    base = (config.DATA_DIR / sandbox / "workspace").resolve()
    pp = PurePosixPath(str(rel).strip())
    if pp.is_absolute():
        parts = pp.parts[1:]  # drop the leading "/"
        if parts and parts[0] == "workspace":
            parts = parts[1:]  # drop the container mount point
            pp = PurePosixPath(*parts) if parts else PurePosixPath()
        else:
            # Absolute path NOT under /workspace -- reject with guidance.
            name = PurePosixPath(rel).name or "filename"
            raise PathError(
                f"path {rel!r} is outside the sandbox workspace. The workspace is "
                f"/workspace inside the container -- /tmp, /home, /root etc. are "
                f"SEPARATE places. Use a relative name (e.g. {name!r}) or start "
                f"with /workspace/ (e.g. /workspace/{name!r})."
            )
    target = (base / pp).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PathError("path traversal rejected")
    return target


def _decode_name(name: str) -> str:
    """Turn a filesystem name (possibly carrying surrogateescaped non-UTF-8 bytes,
    e.g. GBK from a zip) into clean, JSON-safe text."""
    return smart_decode(name.encode("utf-8", "surrogateescape"))


def _resolve(sandbox: str, rel: str) -> Path:
    """Like _safe_path, but if the exact file is missing, fall back to a sibling
    whose decoded name matches the request -- so a Chinese name shown by list_files
    still maps back to its GBK-byte file on disk.  Raises PathError from _safe_path
    if the path is bad."""
    target = _safe_path(sandbox, rel)
    if target.exists():
        return target
    wanted = PurePosixPath(rel).name
    parent = target.parent
    if parent.is_dir():
        for entry in parent.iterdir():
            if _decode_name(entry.name) == wanted:
                return entry
    return target


# --- inline text tools (small files only) ---

def list_dir(sandbox: str, rel: str = ".") -> dict:
    try:
        target = _resolve(sandbox, rel)
    except PathError as e:
        return {"error": str(e)}
    if not target.exists():
        return {"path": rel, "entries": []}
    entries = []
    for p in sorted(target.iterdir(), key=lambda x: x.name):
        if p.name == ".smcp":
            continue
        entries.append(
            {
                "name": _decode_name(p.name),
                "type": "dir" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else None,
            }
        )
    return {"path": rel, "entries": entries}


def read_text(sandbox: str, rel: str) -> dict:
    try:
        target = _resolve(sandbox, rel)
    except PathError as e:
        return {"error": str(e)}
    if not target.is_file():
        return {"error": "not found"}
    size = target.stat().st_size
    if size > config.READ_TEXT_MAX_BYTES:
        return {
            "error": f"file too large for inline read ({size} bytes); use download_file"
        }
    return {"path": rel, "content": smart_decode(target.read_bytes())}


def write_text(sandbox: str, rel: str, content: str) -> dict:
    try:
        target = _safe_path(sandbox, rel)
    except PathError as e:
        return {"error": str(e)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": rel, "size": target.stat().st_size}


# --- HTTP handlers for the signed side-channel ---

async def download(request):
    payload = auth.verify(request.path_params["sig"])
    if not payload or payload.get("op") != "get":
        return PlainTextResponse("forbidden", status_code=403)
    try:
        target = _resolve(payload["sb"], payload["p"])
    except PathError as e:
        return PlainTextResponse(str(e), status_code=400)
    if not target.is_file():
        return PlainTextResponse("not found", status_code=404)
    log.info("HTTP download sandbox=%s path=%s size=%s", payload["sb"], payload["p"], target.stat().st_size)
    return FileResponse(target, filename=_decode_name(target.name))


async def upload(request):
    payload = auth.verify(request.path_params["sig"])
    if not payload or payload.get("op") != "put":
        return PlainTextResponse("forbidden", status_code=403)
    try:
        target = _safe_path(payload["sb"], payload["p"])
    except PathError as e:
        return PlainTextResponse(str(e), status_code=400)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    log.info("HTTP upload sandbox=%s path=%s size=%s", payload["sb"], payload["p"], size)
    return JSONResponse({"ok": True, "path": payload["p"], "size": size})


def _bearer_ok(request) -> bool:
    """True if the request carries the MCP bearer token. Used by the stable
    push/pull endpoints (the gateway bridge holds this token already, since it
    also proxies /mcp), so they need no per-file signed URL."""
    return request.headers.get("authorization", "") == f"Bearer {config.TOKEN}"


async def push(request):
    """Stable bearer-authed upload: a trusted client (e.g. the gateway bridge) POSTs
    file bytes here with ?sandbox=&path= instead of fetching a one-time signed URL
    first. Backs the bridge's by-path upload_file without a per-file signed URL."""
    if not _bearer_ok(request):
        return PlainTextResponse("unauthorized", status_code=401)
    sandbox = request.query_params.get("sandbox", "")
    rel = request.query_params.get("path", "")
    if not sandbox or not rel:
        return PlainTextResponse("missing sandbox/path", status_code=400)
    try:
        target = _safe_path(sandbox, rel)
    except PathError as e:
        return PlainTextResponse(str(e), status_code=400)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    log.info("HTTP push sandbox=%s path=%s size=%s", sandbox, rel, size)
    return JSONResponse({"ok": True, "path": rel, "size": size})


async def pull(request):
    """Stable bearer-authed download counterpart to push()."""
    if not _bearer_ok(request):
        return PlainTextResponse("unauthorized", status_code=401)
    sandbox = request.query_params.get("sandbox", "")
    rel = request.query_params.get("path", "")
    if not sandbox or not rel:
        return PlainTextResponse("missing sandbox/path", status_code=400)
    try:
        target = _resolve(sandbox, rel)
    except PathError as e:
        return PlainTextResponse(str(e), status_code=400)
    if not target.is_file():
        return PlainTextResponse("not found", status_code=404)
    log.info("HTTP pull sandbox=%s path=%s size=%s", sandbox, rel, target.stat().st_size)
    return FileResponse(target, filename=_decode_name(target.name))


async def health(request):
    return JSONResponse({"ok": True})
