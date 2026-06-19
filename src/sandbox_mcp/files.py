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


def _safe_path(sandbox: str, rel: str) -> Path | None:
    """Resolve `rel` under the sandbox workspace, rejecting path traversal."""
    base = (config.DATA_DIR / sandbox / "workspace").resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def _decode_name(name: str) -> str:
    """Turn a filesystem name (possibly carrying surrogateescaped non-UTF-8 bytes,
    e.g. GBK from a zip) into clean, JSON-safe text."""
    return smart_decode(name.encode("utf-8", "surrogateescape"))


def _resolve(sandbox: str, rel: str) -> Path | None:
    """Like _safe_path, but if the exact file is missing, fall back to a sibling
    whose decoded name matches the request -- so a Chinese name shown by list_files
    still maps back to its GBK-byte file on disk."""
    target = _safe_path(sandbox, rel)
    if target is None or target.exists():
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
    target = _resolve(sandbox, rel)
    if target is None:
        return {"error": "bad path"}
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
    target = _resolve(sandbox, rel)
    if target is None or not target.is_file():
        return {"error": "not found"}
    size = target.stat().st_size
    if size > config.READ_TEXT_MAX_BYTES:
        return {
            "error": f"file too large for inline read ({size} bytes); use download_url"
        }
    return {"path": rel, "content": smart_decode(target.read_bytes())}


def write_text(sandbox: str, rel: str, content: str) -> dict:
    target = _safe_path(sandbox, rel)
    if target is None:
        return {"error": "bad path"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": rel, "size": target.stat().st_size}


# --- HTTP handlers for the signed side-channel ---

async def download(request):
    payload = auth.verify(request.path_params["sig"])
    if not payload or payload.get("op") != "get":
        return PlainTextResponse("forbidden", status_code=403)
    target = _resolve(payload["sb"], payload["p"])
    if target is None:
        return PlainTextResponse("bad path", status_code=400)
    if not target.is_file():
        return PlainTextResponse("not found", status_code=404)
    log.info("HTTP download sandbox=%s path=%s size=%s", payload["sb"], payload["p"], target.stat().st_size)
    return FileResponse(target, filename=_decode_name(target.name))


async def upload(request):
    payload = auth.verify(request.path_params["sig"])
    if not payload or payload.get("op") != "put":
        return PlainTextResponse("forbidden", status_code=403)
    target = _safe_path(payload["sb"], payload["p"])
    if target is None:
        return PlainTextResponse("bad path", status_code=400)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    log.info("HTTP upload sandbox=%s path=%s size=%s", payload["sb"], payload["p"], size)
    return JSONResponse({"ok": True, "path": payload["p"], "size": size})


async def health(request):
    return JSONResponse({"ok": True})
