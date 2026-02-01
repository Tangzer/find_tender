import asyncio
import gzip
import hashlib
import httpx
import json
import logging
import random
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, Dict, Callable

from fastapi import FastAPI, Query, HTTPException

Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler("logs/clone_database.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("clone_database")

clone_file_lock = Lock()
CLONE_OPERATIONS: Dict[str, dict] = {}

HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0, pool=60.0)
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=10, max_connections=20)
HTTP_RETRY_STATUS = {429, 502, 503, 504}
HTTP_MAX_ATTEMPTS = 6
HTTP_BACKOFF_CAP = 30


def _ensure_logs_directory():
    Path("logs").mkdir(exist_ok=True)


def _canonical_json_bytes(obj: Any) -> bytes:
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return s.encode("utf-8")


def _content_hash_sha256(obj: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(obj)).hexdigest()


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _write_gzip_atomic(path: Path, raw_bytes: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb") as f:
        f.write(raw_bytes)
    tmp.replace(path)


def _extract_upstream_version_fields(release: dict, page_payload: dict) -> dict:
    upstream = {
        "release_id": release.get("id"),
        "release_date": release.get("date"),
        "page_publishedDate": page_payload.get("publishedDate"),
        "page_date": page_payload.get("date"),
    }
    return {k: v for k, v in upstream.items() if v is not None}


class CloneStore:
    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        self.base_dir = Path("data") / "clones" / operation_id
        self.objects_dir = self.base_dir / "objects" / "sha256"
        self.events_dir = self.base_dir / "events"
        self.status_file = self.base_dir / "status.json"
        self.checkpoint_file = self.base_dir / "checkpoint.json"
        self.manifest_file = self.base_dir / "manifest.json"

        _safe_mkdir(self.objects_dir)
        _safe_mkdir(self.events_dir)

        self.part_index = 1
        self.part_bytes = 0
        self.part_max_bytes = 100 * 1024 * 1024
        self.current_events_path = self.events_dir / f"part-{self.part_index:06d}.ndjson"

    def _rotate_if_needed(self):
        if self.part_bytes >= self.part_max_bytes:
            self.part_index += 1
            self.part_bytes = 0
            self.current_events_path = self.events_dir / f"part-{self.part_index:06d}.ndjson"

    def load_checkpoint(self) -> dict:
        if not self.checkpoint_file.exists():
            return {}
        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.warning("[%s] Failed to read checkpoint; starting fresh", self.operation_id, exc_info=True)
            return {}

    def save_checkpoint(self, checkpoint: dict) -> None:
        with clone_file_lock:
            _atomic_write_json(self.checkpoint_file, checkpoint)

    def save_status(self, status_payload: dict) -> None:
        with clone_file_lock:
            _atomic_write_json(self.status_file, status_payload)

    def read_status(self) -> Optional[dict]:
        if not self.status_file.exists():
            return None
        try:
            with open(self.status_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def write_object_if_missing(self, content_hash: str, obj: dict) -> bool:
        obj_path = self.objects_dir / f"{content_hash}.json.gz"
        if obj_path.exists():
            return False
        raw = _canonical_json_bytes(obj)
        with clone_file_lock:
            if obj_path.exists():
                return False
            _write_gzip_atomic(obj_path, raw)
        return True

    def append_event(self, event: dict) -> None:
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        with clone_file_lock:
            self._rotate_if_needed()
            with open(self.current_events_path, "ab") as f:
                f.write(line)
            self.part_bytes += len(line)

    def finalize_manifest(self, manifest: dict) -> None:
        with clone_file_lock:
            _atomic_write_json(self.manifest_file, manifest)


def _compute_backoff(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after and retry_after.isdigit():
        return max(1, min(float(retry_after), HTTP_BACKOFF_CAP))
    return min((2 ** attempt), HTTP_BACKOFF_CAP)


async def _sleep_with_jitter(seconds: float) -> None:
    await asyncio.sleep(seconds + random.uniform(0, 0.5))


async def get_json_with_retry_client(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    for attempt in range(HTTP_MAX_ATTEMPTS):
        try:
            r = await client.get(url, params=params)

            if r.status_code in HTTP_RETRY_STATUS:
                wait = _compute_backoff(attempt, r.headers.get("Retry-After"))
                logger.warning(
                    "Upstream %s (attempt %s/%s). Waiting %ss...",
                    r.status_code,
                    attempt + 1,
                    HTTP_MAX_ATTEMPTS,
                    wait,
                )
                await _sleep_with_jitter(wait)
                continue

            r.raise_for_status()
            return r.json()

        except httpx.RequestError as e:
            wait = _compute_backoff(attempt, None)
            logger.warning(
                "Network error (attempt %s/%s): %s. Waiting %ss...",
                attempt + 1,
                HTTP_MAX_ATTEMPTS,
                type(e).__name__,
                wait,
            )
            await _sleep_with_jitter(wait)
            if attempt == HTTP_MAX_ATTEMPTS - 1:
                raise HTTPException(status_code=502, detail=f"Network error after retries: {str(e)}")
            continue
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502,
                                detail=f"FindTender error {e.response.status_code}: {e.response.text}") from e

    raise HTTPException(status_code=502, detail="FindTender: exceeded retries")


def _handle_background_task_result(operation_id: str, task):
    try:
        task.result()
    except Exception as e:
        logger.error("[%s] Background task failed: %s", operation_id, str(e), exc_info=True)
        if operation_id in CLONE_OPERATIONS:
            CLONE_OPERATIONS[operation_id].update({"status": "failed", "completed": True, "error": str(e)})


def register_clone_routes(
        app: FastAPI,
        *,
        base_url: str,
        api_version: str,
        stage_type: Any,
        safe_get: Callable[..., Any],
        extract_query_param: Callable[[Optional[str], str], Optional[str]],
        extract_cursor: Callable[[Optional[str]], Optional[str]],
) -> None:
    @app.on_event("startup")
    async def _startup_clone_logging():
        _ensure_logs_directory()
        logger.info("Clone database logging initialized")

    @app.post("/admin/clone_database")
    async def clone_raw_database(
            total: int = Query(-1, ge=-1),
            stages: Optional[stage_type] = Query(None),
            updatedFrom: Optional[str] = Query(None, max_length=19),
            updatedTo: Optional[str] = Query(None, max_length=19),
            background: bool = Query(False),
            operation_id: Optional[str] = Query(None, max_length=128),
            force: bool = Query(False),
    ):
        """
        If operation_id is provided:
          - resume from data/clones/<operation_id>/checkpoint.json
          - reuse same output folder
        If not provided:
          - create a new operation_id
        If completed and force=false:
          - returns stored status without re-running
        """

        # lightweight in-process concurrency guard
        active_bg_ops = sum(1 for op in CLONE_OPERATIONS.values() if not op.get("completed"))
        if background and active_bg_ops >= 2:
            raise HTTPException(status_code=429,
                                detail=f"Too many background operations running ({active_bg_ops}). Max: 2")

        if operation_id:
            store = CloneStore(operation_id)
            # validate that this is a real existing job (must have checkpoint or status)
            if not store.base_dir.exists():
                raise HTTPException(status_code=404, detail=f"operation_id not found on disk: {operation_id}")

            existing_status = store.read_status()
            if existing_status and existing_status.get("status") == "completed" and not force:
                return existing_status

            # mark as running/resumed
            CLONE_OPERATIONS[operation_id] = {
                "started_at": datetime.utcnow(),
                "total_requested": total,
                "stages": stages,
                "status": "running",
                "completed": False,
                "error": None,
                "path": str(store.base_dir),
            }
            store.save_status({
                "operation_id": operation_id,
                "status": "running",
                "resumed_at": datetime.utcnow().isoformat() + "Z",
                "total_requested": total,
                "stages": stages,
                "updatedFrom": updatedFrom,
                "updatedTo": updatedTo,
                "path": str(store.base_dir),
            })

        else:
            operation_id = f"clone_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{hash(str(stages) + str(total)) % 10000}"
            store = CloneStore(operation_id)

            CLONE_OPERATIONS[operation_id] = {
                "started_at": datetime.utcnow(),
                "total_requested": total,
                "stages": stages,
                "status": "running",
                "completed": False,
                "error": None,
                "path": str(store.base_dir),
            }
            store.save_status({
                "operation_id": operation_id,
                "status": "running",
                "started_at": datetime.utcnow().isoformat() + "Z",
                "total_requested": total,
                "stages": stages,
                "updatedFrom": updatedFrom,
                "updatedTo": updatedTo,
                "path": str(store.base_dir),
            })

        if background:
            task = asyncio.create_task(_clone_database_impl(
                store=store,
                total=total,
                stages=stages,
                updatedFrom=updatedFrom,
                updatedTo=updatedTo,
                base_url=base_url,
                api_version=api_version,
                safe_get=safe_get,
                extract_query_param=extract_query_param,
                extract_cursor=extract_cursor,
            ))
            task.add_done_callback(lambda t: _handle_background_task_result(operation_id, t))
            return {
                "status": "background_queued",
                "operation_id": operation_id,
                "path": str(store.base_dir),
                "message": "Clone queued. Check status with /admin/clone_status/{operation_id}",
            }

        return await _clone_database_impl(
            store=store,
            total=total,
            stages=stages,
            updatedFrom=updatedFrom,
            updatedTo=updatedTo,
            base_url=base_url,
            api_version=api_version,
            safe_get=safe_get,
            extract_query_param=extract_query_param,
            extract_cursor=extract_cursor,
        )

    async def _clone_database_impl(
            *,
            store: CloneStore,
            total: int,
            stages: Optional[Any],
            updatedFrom: Optional[str],
            updatedTo: Optional[str],
            base_url: str,
            api_version: str,
            safe_get: Callable[..., Any],
            extract_query_param: Callable[[Optional[str], str], Optional[str]],
            extract_cursor: Callable[[Optional[str]], Optional[str]],
    ):
        operation_id = store.operation_id
        start_time = datetime.utcnow()
        url = f"{base_url}/api/{api_version}/ocdsReleasePackages"

        checkpoint = store.load_checkpoint()
        cursor: Optional[str] = checkpoint.get("cursor")
        pages = int(checkpoint.get("pages", 0))
        received_raw = int(checkpoint.get("received_raw", 0))
        events_written = int(checkpoint.get("events_written", 0))
        objects_written = int(checkpoint.get("objects_written", 0))
        processed_total = int(checkpoint.get("processed_total", 0))

        fixed_updated_from: Optional[str] = checkpoint.get("fixed_updated_from", updatedFrom)
        fixed_updated_to: Optional[str] = checkpoint.get("fixed_updated_to", updatedTo)
        fixed_stages: Optional[Any] = checkpoint.get("fixed_stages", stages)

        remaining = total if total > 0 else float("inf")
        api_limit = 100

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS) as client:
            try:
                while True:
                    if remaining != float("inf") and remaining <= 0:
                        break

                    params: Dict[str, Any] = {"limit": api_limit}
                    if cursor:
                        params["cursor"] = cursor
                    if fixed_stages:
                        params["stages"] = fixed_stages
                    if fixed_updated_from:
                        params["updatedFrom"] = fixed_updated_from
                    if fixed_updated_to:
                        params["updatedTo"] = fixed_updated_to

                    data = await get_json_with_retry_client(client, url, params)

                    if fixed_updated_to is None:
                        fixed_updated_to = extract_query_param(data.get("uri"), "updatedTo")
                        if fixed_updated_to is None:
                            fixed_updated_to = extract_query_param(safe_get(data, "links", "next"), "updatedTo")

                    releases = data.get("releases", []) or []
                    if not releases:
                        break

                    pages += 1
                    received_raw += len(releases)

                    if remaining == float("inf"):
                        page_releases = releases
                    else:
                        page_releases = releases[: int(remaining)]

                    fetched_at = datetime.utcnow().isoformat() + "Z"

                    for rel in page_releases:
                        ocid = rel.get("ocid")
                        content_hash = _content_hash_sha256(rel)

                        if store.write_object_if_missing(content_hash, rel):
                            objects_written += 1

                        store.append_event({
                            "fetched_at": fetched_at,
                            "source": "api",
                            "page": pages,
                            "cursor": cursor,
                            "ocid": ocid,
                            "content_hash": content_hash,
                            "upstream": _extract_upstream_version_fields(rel, data),
                            "page_uri": data.get("uri"),
                        })
                        events_written += 1

                    if remaining != float("inf"):
                        processed_total += len(page_releases)
                        remaining = total - processed_total

                    next_cursor = data.get("nextCursor")
                    if not next_cursor:
                        next_cursor = extract_cursor(safe_get(data, "links", "next"))

                    cursor = next_cursor
                    if not cursor:
                        break

                    store.save_checkpoint({
                        "operation_id": operation_id,
                        "cursor": cursor,
                        "pages": pages,
                        "received_raw": received_raw,
                        "processed_total": processed_total,
                        "events_written": events_written,
                        "objects_written": objects_written,
                        "fixed_updated_from": fixed_updated_from,
                        "fixed_updated_to": fixed_updated_to,
                        "fixed_stages": fixed_stages,
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                    })

                    store.save_status({
                        "operation_id": operation_id,
                        "status": "running",
                        "elapsed_seconds": round((datetime.utcnow() - start_time).total_seconds(), 2),
                        "pages": pages,
                        "received_raw": received_raw,
                        "events_written": events_written,
                        "objects_written": objects_written,
                        "cursor": cursor,
                        "path": str(store.base_dir),
                    })

                elapsed_seconds = round((datetime.utcnow() - start_time).total_seconds(), 2)

                store.finalize_manifest({
                    "operation_id": operation_id,
                    "source": "find-tender-api",
                    "base_url": base_url,
                    "version": api_version,
                    "params": {
                        "total_requested": total if total > 0 else "unlimited",
                        "stages": fixed_stages,
                        "updatedFrom": fixed_updated_from,
                        "updatedTo": fixed_updated_to,
                    },
                    "stats": {
                        "pages": pages,
                        "received_raw": received_raw,
                        "events_written": events_written,
                        "objects_written": objects_written,
                    },
                    "generated_at": datetime.utcnow().isoformat() + "Z",
                    "elapsed_seconds": elapsed_seconds,
                    "layout": {
                        "objects_dir": str(store.objects_dir),
                        "events_dir": str(store.events_dir),
                        "checkpoint": str(store.checkpoint_file),
                    },
                })

                result = {
                    "status": "success",
                    "operation_id": operation_id,
                    "path": str(store.base_dir),
                    "pages": pages,
                    "received_raw": received_raw,
                    "events_written": events_written,
                    "objects_written": objects_written,
                    "elapsed_seconds": elapsed_seconds,
                    "generated_at": datetime.utcnow().isoformat() + "Z",
                    "resume_supported": True,
                }

                CLONE_OPERATIONS[operation_id] = {"status": "completed", "completed": True, "result": result}
                store.save_status({"operation_id": operation_id, "status": "completed", "result": result})
                return result

            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                CLONE_OPERATIONS[operation_id] = {"status": "failed", "completed": True, "error": error_msg}
                store.save_status({"operation_id": operation_id, "status": "failed", "error": error_msg})
                raise HTTPException(status_code=500, detail=f"Clone failed: {error_msg}")

    @app.get("/admin/clone_status/{operation_id}")
    def clone_status(operation_id: str):
        status_path = Path("data") / "clones" / operation_id / "status.json"
        if status_path.exists():
            with open(status_path, "r", encoding="utf-8") as f:
                return json.load(f)

        if operation_id not in CLONE_OPERATIONS:
            raise HTTPException(status_code=404, detail="Operation not found")

        return CLONE_OPERATIONS[operation_id]
