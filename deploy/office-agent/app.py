from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger("office-agent")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

WORKSPACE = Path(os.environ.get("OFFICE_AGENT_WORKSPACE", "/workspace")).resolve()
OFFICECLI = os.environ.get("OFFICECLI_BIN", "/usr/local/bin/officecli")
COMMAND_TIMEOUT_S = max(5.0, min(120.0, float(os.environ.get("OFFICE_AGENT_TIMEOUT_S", "30"))))
INTENT_TTL_S = max(15, min(300, int(os.environ.get("OFFICE_AGENT_INTENT_TTL_S", "60"))))
MAX_OUTPUT_BYTES = max(4096, min(1_000_000, int(os.environ.get("OFFICE_AGENT_MAX_OUTPUT_BYTES", "65536"))))
MAX_ARTIFACT_BYTES = max(1_000_000, min(50_000_000, int(os.environ.get("OFFICE_AGENT_MAX_ARTIFACT_BYTES", "15000000"))))
ARTIFACT_TTL_S = max(300, min(7 * 86400, int(os.environ.get("OFFICE_AGENT_ARTIFACT_TTL_S", "86400"))))
SERVICE_TOKEN = os.environ.get("OFFICE_AGENT_TOKEN", "").strip()

OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
INPUT_EXTENSIONS = OFFICE_EXTENSIONS | {".csv", ".json", ".png", ".jpg", ".jpeg", ".webp"}
PROP_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
ARTIFACT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
FORBIDDEN_WORDS = {"raw", "raw-set", "watch", "unwatch", "plugins", "install", "config", "mcp", "load_skill"}

STATE_DIR = WORKSPACE / ".office-agent"
ARTIFACT_DIR = STATE_DIR / "artifacts"
BACKUP_DIR = STATE_DIR / "backups"
COMMIT_DIR = Path(os.environ.get("OFFICE_AGENT_COMMIT_DIR", "/tmp/office-agent-commit")).resolve()

app = FastAPI(title="local-office-agent", docs_url=None, redoc_url=None, openapi_url=None)
mutation_lock = asyncio.Lock()
startup_error = ""
officecli_version = ""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListRequest(StrictModel):
    directory: str = ""
    limit: int = Field(default=100, ge=1, le=250)


class DocumentRequest(StrictModel):
    path: str


class InspectRequest(DocumentRequest):
    mode: Literal["outline", "stats", "issues", "text", "get", "query"] = "outline"
    target: str = "/"
    depth: int = Field(default=2, ge=0, le=6)
    limit: int = Field(default=100, ge=1, le=250)


class RenderRequest(DocumentRequest):
    format: Literal["html", "screenshot"] = "html"
    page: int | None = Field(default=None, ge=1, le=500)


class ApplyRequest(StrictModel):
    requestId: str = Field(min_length=1, max_length=160)
    operation: Literal["create", "set", "add", "remove", "move"]
    path: str
    target: str = "/"
    parent: str = "/"
    elementType: str | None = Field(default=None, max_length=80)
    props: dict[str, str | int | float | bool] = Field(default_factory=dict)
    index: int | None = Field(default=None, ge=0, le=100000)

    @field_validator("target", "parent")
    @classmethod
    def validate_document_selector(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
            raise ValueError("Document path/selector is invalid.")
        return value

    @field_validator("props")
    @classmethod
    def validate_props(cls, value: dict[str, str | int | float | bool]) -> dict[str, str | int | float | bool]:
        if len(value) > 32:
            raise ValueError("At most 32 properties are allowed.")
        clean: dict[str, str | int | float | bool] = {}
        for key, raw in value.items():
            if not PROP_KEY_RE.fullmatch(key):
                raise ValueError(f"Invalid property name: {key}")
            if isinstance(raw, str) and len(raw) > 2000:
                raise ValueError(f"Property value is too long: {key}")
            clean[key] = raw
        return clean


class IntentRequest(StrictModel):
    intentId: str = Field(pattern=r"^[a-f0-9]{32}$")


@dataclass
class PendingIntent:
    request: ApplyRequest
    request_hash: str
    created_at: float
    expires_at: float


@dataclass
class CompletedRequest:
    request_hash: str
    result: dict[str, Any]
    expires_at: float


pending_intents: dict[str, PendingIntent] = {}
pending_by_request: dict[str, str] = {}
completed_requests: dict[str, CompletedRequest] = {}
artifacts: dict[str, tuple[Path, float]] = {}


@app.middleware("http")
async def require_service_token(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if not SERVICE_TOKEN:
        return JSONResponse(status_code=503, content={"detail": "Office agent service token is not configured."})
    supplied = request.headers.get("x-office-agent-token", "")
    if not secrets.compare_digest(supplied, SERVICE_TOKEN):
        return JSONResponse(status_code=403, content={"detail": "Office agent service authentication failed."})
    return await call_next(request)


def _safe_process_env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp/office-home",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "OFFICECLI_SKIP_UPDATE": "1",
        "CHROME_BIN": os.environ.get("CHROME_BIN", "/usr/bin/chromium"),
    }


def _validate_relative_path(raw: str, *, office_only: bool, must_exist: bool) -> tuple[Path, str]:
    value = (raw or "").strip().replace("\\", "/")
    if not value or len(value) > 240 or any(ord(char) < 32 for char in value):
        raise HTTPException(status_code=400, detail="Workspace path is invalid.")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=403, detail="Workspace path must stay inside the Office workspace.")
    if relative.parts and relative.parts[0].lower() == ".office-agent":
        raise HTTPException(status_code=403, detail="Office agent state is not part of the document workspace.")

    candidate = (WORKSPACE / relative).resolve(strict=False)
    try:
        candidate.relative_to(WORKSPACE)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Workspace path escapes the Office workspace.") from exc

    allowed = OFFICE_EXTENSIONS if office_only else INPUT_EXTENSIONS
    if candidate.suffix.lower() not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {candidate.suffix or '(none)'}")
    if must_exist and (not candidate.is_file() or candidate.is_symlink() and candidate.resolve() != candidate):
        raise HTTPException(status_code=404, detail="Office document was not found.")
    if must_exist:
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(WORKSPACE)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Workspace symlink escapes the Office workspace.") from exc
        candidate = resolved
    return candidate, candidate.relative_to(WORKSPACE).as_posix()


def _resolve_directory(raw: str) -> Path:
    value = (raw or "").strip().replace("\\", "/")
    if len(value) > 240 or any(ord(char) < 32 for char in value):
        raise HTTPException(status_code=400, detail="Workspace directory is invalid.")
    relative = Path(value or ".")
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=403, detail="Directory must stay inside the Office workspace.")
    if relative.parts and relative.parts[0].lower() == ".office-agent":
        raise HTTPException(status_code=403, detail="Office agent state is not part of the document workspace.")
    try:
        candidate = (WORKSPACE / relative).resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace directory was not found.") from exc
    try:
        candidate.relative_to(WORKSPACE)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Directory escapes the Office workspace.") from exc
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Workspace directory was not found.")
    return candidate


def _format_prop(value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _run_cli(arguments: list[str], *, timeout_s: float | None = None) -> dict[str, Any]:
    if any(argument.lower() in FORBIDDEN_WORDS for argument in arguments[:2]):
        raise HTTPException(status_code=403, detail="OfficeCLI operation is not allowed.")
    argv = [OFFICECLI, *arguments]
    process = subprocess.Popen(
        argv,
        cwd=WORKSPACE,
        env=_safe_process_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s or COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise HTTPException(status_code=504, detail="OfficeCLI command timed out.") from exc

    stdout = stdout[:MAX_OUTPUT_BYTES]
    stderr = stderr[: min(MAX_OUTPUT_BYTES, 8192)]
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
        raise HTTPException(status_code=422, detail=f"OfficeCLI failed: {detail[:600] or f'exit {process.returncode}'}")

    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        return {"ok": True}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": True, "text": text[:12000]}
    if isinstance(parsed, dict):
        return parsed
    return {"ok": True, "data": parsed}


def _run_json(arguments: list[str]) -> dict[str, Any]:
    return _run_cli([*arguments, "--json"])


def _canonical_request(request: ApplyRequest) -> tuple[str, str]:
    payload = request.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    summary = f"{request.operation} {request.path}"
    if request.operation != "create":
        summary += f" at {request.target}"
    return digest, summary


def _expire_state() -> None:
    now = time.time()
    for intent_id, intent in list(pending_intents.items()):
        if intent.expires_at <= now:
            pending_intents.pop(intent_id, None)
            pending_by_request.pop(intent.request.requestId, None)
    for request_id, result in list(completed_requests.items()):
        if result.expires_at <= now:
            completed_requests.pop(request_id, None)
    for artifact_id, (path, expires_at) in list(artifacts.items()):
        if expires_at <= now or not path.is_file():
            artifacts.pop(artifact_id, None)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _mutation_arguments(request: ApplyRequest, document: Path) -> list[str]:
    if request.operation == "create":
        return ["create", str(document)]
    if request.operation == "set":
        argv = ["set", str(document), request.target]
    elif request.operation == "add":
        if not request.elementType or not PROP_KEY_RE.fullmatch(request.elementType):
            raise HTTPException(status_code=400, detail="add requires a valid elementType.")
        argv = ["add", str(document), request.parent, "--type", request.elementType]
    elif request.operation == "remove":
        argv = ["remove", str(document), request.target]
    elif request.operation == "move":
        argv = ["move", str(document), request.target, "--to", request.parent]
        if request.index is not None:
            argv.extend(["--index", str(request.index)])
    else:  # pragma: no cover - guarded by Literal
        raise HTTPException(status_code=400, detail="Unsupported Office operation.")

    for key, value in request.props.items():
        argv.extend(["--prop", f"{key}={_format_prop(value)}"])
    return argv


def _validate_document(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = _run_json(["validate", str(path)])
    issues = _run_json(["view", str(path), "issues", "--limit", "50"])
    return validation, issues


def _result_succeeded(result: dict[str, Any]) -> bool:
    if "success" in result:
        return result.get("success") is True
    return result.get("ok") is True


def _assert_document_valid(validation: dict[str, Any], issues: dict[str, Any]) -> None:
    # `validate` is the integrity gate. `view issues` also reports non-fatal style
    # suggestions, which are returned to the caller but must not roll back a valid edit.
    if not _result_succeeded(validation) or not _result_succeeded(issues):
        raise HTTPException(status_code=422, detail="Office document validation failed; no workspace change was kept.")


def _file_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _sanitize_cli_result(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        clean = value
        for internal, public in replacements.items():
            clean = clean.replace(internal, public)
        return clean
    if isinstance(value, dict):
        return {key: _sanitize_cli_result(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_cli_result(item, replacements) for item in value]
    return value


def _wait_for_committed_copy(path: Path, expected: tuple[int, str]) -> bool:
    for delay in (0.0, 0.05, 0.15, 0.3):
        if delay:
            time.sleep(delay)
        try:
            if path.is_file() and _file_sha256(path) == expected:
                return True
        except OSError:
            continue
    return False


def _restore_document(source: Path, backup: Path | None) -> None:
    source.unlink(missing_ok=True)
    if backup is None:
        return
    shutil.copy2(backup, source)
    expected = _file_sha256(backup)
    if not _wait_for_committed_copy(source, expected):
        raise OSError("Office document backup could not be restored.")


def _execute_mutation(request: ApplyRequest) -> dict[str, Any]:
    creating = request.operation == "create"
    source, relative = _validate_relative_path(request.path, office_only=True, must_exist=not creating)
    if creating and source.exists():
        raise HTTPException(status_code=409, detail="Refusing to overwrite an existing document.")
    source.parent.mkdir(parents=True, exist_ok=True)

    temp = source.with_name(f".{source.stem}.{uuid.uuid4().hex}.tmp{source.suffix}")
    COMMIT_DIR.mkdir(parents=True, exist_ok=True)
    staged_copy = COMMIT_DIR / f"{uuid.uuid4().hex}{source.suffix}"
    backup: Path | None = None
    commit_attempted = False
    try:
        if not creating:
            shutil.copy2(source, temp)
        result = _run_json(_mutation_arguments(request, temp))
        validation, issues = _validate_document(temp)
        _assert_document_valid(validation, issues)
        shutil.copy2(temp, staged_copy)
        expected = _file_sha256(staged_copy)
        if not creating:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup = BACKUP_DIR / f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{source.name}"
            shutil.copy2(source, backup)
        commit_attempted = True
        os.replace(temp, source)
        if not _wait_for_committed_copy(source, expected):
            logger.warning("atomic replace was not immediately visible; using validated copy fallback path=%s", relative)
            source.unlink(missing_ok=True)
            shutil.copy2(staged_copy, source)
        if not _wait_for_committed_copy(source, expected):
            raise OSError("Committed Office document is not readable or does not match the validated copy.")
        validation, issues = _validate_document(source)
        _assert_document_valid(validation, issues)
        public_result = _sanitize_cli_result(
            result,
            {
                str(temp): relative,
                str(staged_copy): relative,
                str(source): relative,
            },
        )
        return {
            "status": "completed",
            "path": relative,
            "operation": request.operation,
            "result": public_result,
            "validation": validation,
            "issues": issues,
            "backupCreated": backup is not None,
        }
    except Exception:
        if commit_attempted:
            try:
                _restore_document(source, backup)
            except Exception as rollback_error:
                logger.exception("office mutation rollback failed path=%s", relative)
                raise HTTPException(
                    status_code=500,
                    detail="Office mutation failed and the previous document could not be restored automatically.",
                ) from rollback_error
        raise
    finally:
        temp.unlink(missing_ok=True)
        staged_copy.unlink(missing_ok=True)


@app.on_event("startup")
async def initialize() -> None:
    global startup_error, officecli_version
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        COMMIT_DIR.mkdir(parents=True, exist_ok=True)
        version_result = await asyncio.to_thread(_run_cli, ["--version"], timeout_s=10)
        officecli_version = str(version_result.get("text") or version_result.get("version") or "1.0.135")[:80]
        startup_error = ""
        _expire_state()
        logger.info("office agent ready version=%s", officecli_version)
    except Exception as exc:  # pragma: no cover - exercised by container health checks
        startup_error = f"{exc.__class__.__name__}: {exc}"
        logger.exception("office agent initialization failed")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ready": not bool(startup_error) and bool(SERVICE_TOKEN),
        "status": "ready" if not startup_error and SERVICE_TOKEN else "error",
        "version": officecli_version or "1.0.135",
        "writeApprovalRequired": True,
        **(
            {"detail": startup_error[:300]}
            if startup_error
            else ({"detail": "Office agent service token is not configured."} if not SERVICE_TOKEN else {})
        ),
    }


@app.post("/v1/list")
async def list_documents(request: ListRequest) -> dict[str, Any]:
    directory = _resolve_directory(request.directory)
    files: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if len(files) >= request.limit:
            break
        if STATE_DIR in path.parents or path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in INPUT_EXTENSIONS:
            continue
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(WORKSPACE).as_posix()
        except ValueError:
            continue
        stat = resolved.stat()
        files.append({"path": relative, "sizeBytes": stat.st_size, "modifiedAt": stat.st_mtime})
    return {"files": files, "truncated": len(files) >= request.limit}


@app.post("/v1/inspect")
async def inspect_document(request: InspectRequest) -> dict[str, Any]:
    path, relative = _validate_relative_path(request.path, office_only=True, must_exist=True)
    if request.mode in {"outline", "stats", "issues", "text"}:
        argv = ["view", str(path), request.mode]
        if request.mode == "issues":
            argv.extend(["--limit", str(request.limit)])
        elif request.mode == "text":
            argv.extend(["--max-lines", str(request.limit)])
    elif request.mode == "get":
        argv = ["get", str(path), request.target, "--depth", str(request.depth)]
    else:
        argv = ["query", str(path), request.target]
    result = await asyncio.to_thread(_run_json, argv)
    return {"path": relative, "mode": request.mode, "result": result}


@app.post("/v1/validate")
async def validate_document(request: DocumentRequest) -> dict[str, Any]:
    path, relative = _validate_relative_path(request.path, office_only=True, must_exist=True)
    validation, issues = await asyncio.to_thread(_validate_document, path)
    return {"path": relative, "validation": validation, "issues": issues}


@app.post("/v1/render")
async def render_document(request: RenderRequest) -> dict[str, Any]:
    path, relative = _validate_relative_path(request.path, office_only=True, must_exist=True)
    artifact_id = uuid.uuid4().hex
    suffix = ".html" if request.format == "html" else ".png"
    output = ARTIFACT_DIR / f"{artifact_id}{suffix}"
    argv = ["view", str(path), request.format, "-o", str(output)]
    if request.page is not None:
        argv.extend(["--page", str(request.page)])
    result = await asyncio.to_thread(_run_json, argv)
    if not output.is_file():
        raise HTTPException(status_code=502, detail="OfficeCLI did not produce the requested render artifact.")
    size = output.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        output.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="Rendered Office artifact is empty or too large.")
    artifacts[artifact_id] = (output, time.time() + ARTIFACT_TTL_S)
    return {
        "path": relative,
        "format": request.format,
        "artifactId": artifact_id,
        "filename": f"{path.stem}-{request.format}{suffix}",
        "sizeBytes": size,
        "result": result,
    }


@app.get("/v1/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    _expire_state()
    if not ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise HTTPException(status_code=404, detail="Artifact was not found.")
    entry = artifacts.get(artifact_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Artifact was not found or expired.")
    path, _ = entry
    if not path.is_file() or path.parent.resolve() != ARTIFACT_DIR.resolve():
        raise HTTPException(status_code=404, detail="Artifact was not found.")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name, headers={"Cache-Control": "no-store"})


@app.post("/v1/prepare")
async def prepare_mutation(request: ApplyRequest) -> dict[str, Any]:
    _expire_state()
    creating = request.operation == "create"
    _, relative = _validate_relative_path(request.path, office_only=True, must_exist=not creating)
    request.path = relative
    request_hash, summary = _canonical_request(request)

    completed = completed_requests.get(request.requestId)
    if completed:
        if completed.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="requestId was already used for a different mutation.")
        return {"status": "completed", "requestHash": request_hash, "result": completed.result}

    existing_id = pending_by_request.get(request.requestId)
    if existing_id:
        existing = pending_intents.get(existing_id)
        if existing and existing.request_hash == request_hash:
            return {
                "status": "approval_required",
                "intentId": existing_id,
                "requestHash": request_hash,
                "summary": summary,
                "path": relative,
                "expiresAt": existing.expires_at,
            }
        raise HTTPException(status_code=409, detail="requestId is already pending with different arguments.")

    intent_id = uuid.uuid4().hex
    now = time.time()
    pending_intents[intent_id] = PendingIntent(request, request_hash, now, now + INTENT_TTL_S)
    pending_by_request[request.requestId] = intent_id
    return {
        "status": "approval_required",
        "intentId": intent_id,
        "requestHash": request_hash,
        "summary": summary,
        "path": relative,
        "expiresAt": now + INTENT_TTL_S,
    }


@app.post("/v1/execute")
async def execute_mutation(request: IntentRequest) -> dict[str, Any]:
    _expire_state()
    intent = pending_intents.pop(request.intentId, None)
    if not intent:
        raise HTTPException(status_code=410, detail="Office mutation approval expired, was cancelled, or was already used.")
    pending_by_request.pop(intent.request.requestId, None)
    if intent.expires_at <= time.time():
        raise HTTPException(status_code=410, detail="Office mutation approval expired.")

    async with mutation_lock:
        result = await asyncio.to_thread(_execute_mutation, intent.request)
    completed_requests[intent.request.requestId] = CompletedRequest(
        request_hash=intent.request_hash,
        result=result,
        expires_at=time.time() + 600,
    )
    logger.info("office mutation completed operation=%s path=%s", intent.request.operation, intent.request.path)
    return {"requestHash": intent.request_hash, **result}


@app.post("/v1/cancel")
async def cancel_mutation(request: IntentRequest) -> dict[str, Any]:
    intent = pending_intents.pop(request.intentId, None)
    if intent:
        pending_by_request.pop(intent.request.requestId, None)
    return {"cancelled": bool(intent)}
