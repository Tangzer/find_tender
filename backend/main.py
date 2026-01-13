import os, json, httpx, asyncio, asyncpg, hashlib
from pathlib import Path
from typing import Optional, Literal, Any, Dict
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel
from datetime import datetime
from urllib.parse import urlparse, parse_qs

load_dotenv()

FIND_TENDER_BASE_URL = os.getenv("FIND_TENDER_BASE_URL", "https://www.find-tender.service.gov.uk")
FIND_TENDER_VERSION = os.getenv("FIND_TENDER_VERSION", "1.0")
DATABASE_URL = os.getenv("DATABASE_URL")  # ex: postgresql://user:pass@localhost:5432/find_tender

Stage = Literal["planning", "tender", "award"]

app = FastAPI(title="Tender MVP API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchResponseItem(BaseModel):
    ocid: str
    title: Optional[str] = None
    description: Optional[str] = None
    published_at: Optional[str] = None
    url: Optional[str] = None
    score: float


@app.on_event("startup")
async def _startup_db_pool():
    if not DATABASE_URL:
        # Permet de démarrer l'API même si la DB n'est pas configurée
        app.state.db_pool = None
        return
    app.state.db_pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=60,
    )


@app.on_event("shutdown")
async def _shutdown_db_pool():
    pool = getattr(app.state, "db_pool", None)
    if pool:
        await pool.close()


async def get_json_with_retry(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch JSON with retry logic for rate limits and network timeouts."""
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(6):
            try:
                r = await client.get(url, params=params)

                if r.status_code in (429, 503):
                    retry_after = r.headers.get("Retry-After")
                    wait = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
                    logger.warning(f"Rate limited (attempt {attempt + 1}/6). Waiting {wait}s...")
                    await asyncio.sleep(max(1, wait))
                    continue

                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as e:
                    detail = r.text
                    raise HTTPException(status_code=502, detail=f"FindTender error {r.status_code}: {detail}") from e

                return r.json()

            except (httpx.ReadTimeout, httpx.ConnectError, httpx.TimeoutException) as e:
                # Network timeout/connection errors - retry with backoff
                wait = 2 ** attempt
                logger.warning(f"Network timeout (attempt {attempt + 1}/6): {type(e).__name__}. Waiting {wait}s...")
                await asyncio.sleep(wait)
                if attempt == 5:  # Last attempt
                    raise HTTPException(status_code=502, detail=f"Network timeout after 6 attempts: {str(e)}")
                continue

    raise HTTPException(status_code=502, detail="FindTender: exceeded retries")


def _require_db_pool():
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL not configured (PostgreSQL is required for this endpoint)",
        )
    return pool


def _hash_source(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()


def _safe_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _build_full_text(release: dict) -> str:
    parts = []
    ocid = release.get("ocid")
    if ocid:
        parts.append(ocid)

    tender = release.get("tender", {})
    parts.extend([
        tender.get("title"),
        tender.get("description"),
        tender.get("mainProcurementCategory"),
    ])

    buyer = release.get("buyer", {})
    parts.append(buyer.get("name"))

    # CPV codes
    items = tender.get("items", []) or []
    for it in items:
        for c in it.get("additionalClassifications", []) or []:
            if c.get("id"):
                parts.append(c.get("id"))
            if c.get("description"):
                parts.append(c.get("description"))

    # Lots titles/descriptions
    for lot in tender.get("lots", []) or []:
        parts.append(lot.get("title"))
        parts.append(lot.get("description"))

    return "\n".join([p for p in parts if p])


def _extract_query_param(url: Optional[str], name: str) -> Optional[str]:
    if not url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
        val = qs.get(name)
        return val[0] if val else None
    except Exception:
        return None


def _extract_cursor(next_url: Optional[str]) -> Optional[str]:
    if not next_url:
        return None
    try:
        qs = parse_qs(urlparse(next_url).query)
        cur = qs.get("cursor")
        return cur[0] if cur else None
    except Exception:
        return None


@app.get("/")
def base():
    return {"Working ?": "Yes"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/search", response_model=list[SearchResponseItem])
async def search(
        q: str = Query(..., min_length=2, max_length=200),
        mode: Literal["exact", "near"] = Query("exact"),
        limit: int = Query(25, ge=1, le=100),
):
    """Search in local PostgreSQL table `tenders`.

    - mode=exact: phrase search (ordre des mots)
    - mode=near: fuzzy search (tolère variantes / petites fautes)

    Table attendue:
      tenders(ocid, title, description, full_text, published_at, url)
    """
    pool = _require_db_pool()

    async with pool.acquire() as conn:
        if mode == "exact":
            sql = """
                  SELECT ocid,
                         title,
                         description,
                         published_at,
                         url,
                         ts_rank_cd(to_tsvector('english', coalesce(full_text, '')),
                                    phraseto_tsquery('english', $1)) AS score
                  FROM tenders
                  WHERE to_tsvector('english', coalesce(full_text, '')) @@ phraseto_tsquery('english'
                      , $1)
                  ORDER BY score DESC, published_at DESC NULLS LAST
                      LIMIT $2; \
                  """
            rows = await conn.fetch(sql, q, limit)
        else:
            # near: combine full-text + trigram similarity (pg_trgm)
            sql = """
                  SELECT ocid,
                         title,
                         description,
                         published_at,
                         url,
                         GREATEST(
                                 ts_rank_cd(to_tsvector('english', coalesce(full_text, '')),
                                            websearch_to_tsquery('english', $1)),
                                 similarity(coalesce(full_text, ''), $1)
                         ) AS score
                  FROM tenders
                  WHERE to_tsvector('english', coalesce(full_text, '')) @@ websearch_to_tsquery('english'
                      , $1)
                     OR similarity(coalesce (full_text
                      , '')
                      , $1)
                      > 0.20
                  ORDER BY score DESC, published_at DESC NULLS LAST
                      LIMIT $2; \
                  """
            rows = await conn.fetch(sql, q, limit)

    return [
        SearchResponseItem(
            ocid=r["ocid"],
            title=r["title"],
            description=r["description"],
            published_at=r["published_at"].isoformat() if r["published_at"] else None,
            url=r["url"],
            score=float(r["score"] or 0.0),
        )
        for r in rows
    ]


@app.get("/tenders")
async def list_tenders(
        limit: int = Query(5, ge=1, le=100),
        cursor: Optional[str] = Query(None, max_length=300),
        updatedFrom: Optional[str] = Query(None, max_length=19),  # "YYYY-MM-DDTHH:MM:SS"
        updatedTo: Optional[str] = Query(None, max_length=19),
        stages: Optional[Stage] = Query(None),
):
    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages"
    params = {
        "limit": limit,
        "cursor": cursor,
        "updatedFrom": updatedFrom,
        "updatedTo": updatedTo,
        "stages": stages,
    }
    params = {k: v for k, v in params.items() if v is not None}

    data = await get_json_with_retry(url, params)

    releases = data["releases"]
    return data


@app.get("/tenders/{notice_or_ocid}")
async def get_tender(notice_or_ocid: str):
    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages/{notice_or_ocid}"
    data = await get_json_with_retry(url, {})
    return data


@app.post("/admin/ingest/first")
async def ingest_first_notices(limit: int = Query(20, ge=1, le=100)):
    """Fetch the first `limit` notices from Find a Tender and upsert them into PostgreSQL.
    This is a V1 bootstrap endpoint to quickly populate the DB.
    """
    pool = _require_db_pool()

    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages"
    params = {"limit": limit}
    data = await get_json_with_retry(url, params)
    releases = data.get("releases", [])

    inserted = 0
    updated = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for rel in releases:
                ocid = rel.get("ocid")
                if not ocid:
                    continue

                tender = rel.get("tender", {})
                buyer = rel.get("buyer", {})

                title = tender.get("title")
                description = tender.get("description")

                # Published date (fallbacks)
                published_at = rel.get("date") or data.get("publishedDate")
                if published_at:
                    try:
                        published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                    except Exception:
                        published_at = None

                # URL (from documents if present)
                url_doc = None
                for doc in tender.get("documents", []) or []:
                    if doc.get("url"):
                        url_doc = doc.get("url")
                        break

                full_text = _build_full_text(rel)
                source_hash = _hash_source(rel)

                sql = """
                      INSERT INTO tenders (ocid, title, description, full_text, published_at, url, source_hash)
                      VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (ocid)
                DO
                      UPDATE SET
                          title = EXCLUDED.title,
                          description = EXCLUDED.description,
                          full_text = EXCLUDED.full_text,
                          published_at = EXCLUDED.published_at,
                          url = EXCLUDED.url,
                          source_hash = EXCLUDED.source_hash,
                          updated_at = NOW()
                          RETURNING (xmax = 0) AS inserted; \
                      """

                row = await conn.fetchrow(
                    sql,
                    ocid,
                    title,
                    description,
                    full_text,
                    published_at,
                    url_doc,
                    source_hash,
                )

                if row and row["inserted"]:
                    inserted += 1
                else:
                    updated += 1

    return {
        "requested": limit,
        "received": len(releases),
        "inserted": inserted,
        "updated": updated,
    }


@app.post("/admin/ingest")
async def ingest_notices(
        total: int = Query(200, ge=1, le=5000),
        stages: Optional[Stage] = Query(None),
        updatedFrom: Optional[str] = Query(None, max_length=19),
        updatedTo: Optional[str] = Query(None, max_length=19),
):
    """Ingest `total` notices by paging through the API using `cursor`.

    - L'API Find a Tender limite `limit` à 100 par page.
    - On boucle donc page par page avec `cursor` jusqu'à avoir `total` notices (ou jusqu'à épuisement).

    Params (optionnels):
      - stages: planning|tender|award
      - updatedFrom/updatedTo: "YYYY-MM-DDTHH:MM:SS"
    """
    pool = _require_db_pool()

    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages"

    remaining = total
    api_limit = 100 if total > 100 else total
    cursor: Optional[str] = None
    received_total = 0
    inserted_total = 0
    updated_total = 0
    pages = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Freeze pagination parameters across all pages
            fixed_updated_to: Optional[str] = updatedTo
            fixed_updated_from: Optional[str] = updatedFrom
            fixed_stages: Optional[Stage] = stages
            while remaining > 0:
                params = {"limit": api_limit}
                if cursor:
                    params["cursor"] = cursor
                if fixed_stages:
                    params["stages"] = fixed_stages
                if fixed_updated_from:
                    params["updatedFrom"] = fixed_updated_from
                if fixed_updated_to:
                    params["updatedTo"] = fixed_updated_to

                # print(f"CURSOR : [{cursor}]")

                data = await get_json_with_retry(url, params)
                if fixed_updated_to is None:
                    # Sets an implicit updatedTo when not provided.
                    # We MUST reuse that same updatedTo for all subsequent cursor requests to avoid duplicates & errors
                    fixed_updated_to = _extract_query_param(data.get("uri"), "updatedTo")
                    if fixed_updated_to is None:
                        fixed_updated_to = _extract_query_param(_safe_get(data, "links", "next"), "updatedTo")
                releases = data.get("releases", []) or []

                if not releases:
                    break

                pages += 1
                received_total += len(releases)

                page_releases = releases[:remaining]

                for rel in page_releases:
                    ocid = rel.get("ocid")
                    if not ocid:
                        continue

                    tender = rel.get("tender", {})

                    title = tender.get("title")
                    description = tender.get("description")

                    published_at = rel.get("date") or data.get("publishedDate")
                    if published_at:
                        try:
                            published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                        except Exception:
                            published_at = None

                    url_doc = None
                    for doc in tender.get("documents", []) or []:
                        if doc.get("url"):
                            url_doc = doc.get("url")
                            break

                    full_text = _build_full_text(rel)
                    source_hash = _hash_source(rel)

                    sql = """
                          INSERT INTO tenders (ocid, title, description, full_text, published_at, url, source_hash)
                          VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (ocid)
                    DO
                          UPDATE SET
                              title = EXCLUDED.title,
                              description = EXCLUDED.description,
                              full_text = EXCLUDED.full_text,
                              published_at = EXCLUDED.published_at,
                              url = EXCLUDED.url,
                              source_hash = EXCLUDED.source_hash,
                              updated_at = NOW()
                              RETURNING (xmax = 0) AS inserted; \
                          """

                    row = await conn.fetchrow(
                        sql,
                        ocid,
                        title,
                        description,
                        full_text,
                        published_at,
                        url_doc,
                        source_hash,
                    )

                    if row and row["inserted"]:
                        inserted_total += 1
                    else:
                        updated_total += 1

                remaining -= len(page_releases)
                if remaining <= 0:
                    break

                cursor = data.get("nextCursor")
                if not cursor:
                    next_url = _safe_get(data, "links", "next")
                    cursor = _extract_cursor(next_url)
                if not cursor:
                    break

    return {
        "requested": total,
        "received": received_total,
        "pages": pages,
        "inserted": inserted_total,
        "updated": updated_total,
        "next_cursor": cursor,
    }

# <=============== NEW ===============>

import logging
from datetime import datetime
from threading import Lock

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler("logs/clone_database.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("clone_database")

# Global lock to prevent concurrent writes to JSON file
clone_file_lock = Lock()

# Track active clone operations
CLONE_OPERATIONS: Dict[str, dict] = {}


def _ensure_logs_directory():
    """Create the logs directory if it doesn't exist."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)


@app.on_event("startup")
async def _startup_clone_logging():
    """Initialize logging on app startup."""
    _ensure_logs_directory()
    logger.info("Clone database logging initialized")

def _load_existing_contracts() -> tuple[Dict[str, dict], set[str]]:
    """
    Load existing contracts from the JSON dump file.
    Returns:
      - contracts_dict: Dictionary with ocid as key
      - hashes_set: Set of content hashes for fast duplicate detection
    """
    output_file = Path("data") / "tender_contracts_dump.json"
    if not output_file.exists():
        logger.info("No existing dump file found. Starting fresh.")
        return {}, set()

    try:
        with clone_file_lock:  # Prevent concurrent reads during writes
            with open(output_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        contracts = data.get("tender_contracts", [])
        contracts_dict = {}
        hashes_set = set()

        for c in contracts:
            ocid = c.get("ocid")
            if ocid:
                contracts_dict[ocid] = c
                content_hash = _hash_source(c)
                hashes_set.add(content_hash)

        logger.info(f"Loaded {len(contracts_dict)} existing contracts from file")
        return contracts_dict, hashes_set
    except Exception as e:
        logger.error(f"Error loading existing contracts: {e}. Starting fresh.", exc_info=True)
        return {}, set()


def _save_contracts_to_file(contracts_dict: Dict[str, dict], total_requested: int,
                            received_raw: int, pages: int, skipped_count: int,
                            start_time: datetime):
    """
    Save the contracts dictionary to the JSON dump file with metadata.
    Uses file locking to prevent concurrent writes.
    """
    final_contracts = list(contracts_dict.values())
    unique_count = len(final_contracts)
    elapsed_time = (datetime.utcnow() - start_time).total_seconds()

    result = {
        "tender_contracts": final_contracts,
        "meta": {
            "total": unique_count,
            "requested": total_requested if total_requested > 0 else "unlimited",
            "received_raw": received_raw,
            "pages": pages,
            "duplicates_skipped": skipped_count,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "elapsed_seconds": round(elapsed_time, 2),
        },
    }

    # Ensure data directory exists
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    # Write to JSON file with lock to prevent concurrent writes
    output_file = data_dir / "tender_contracts_dump.json"
    try:
        with clone_file_lock:
            # Write to temporary file first, then rename (atomic operation)
            temp_file = output_file.with_suffix('.json.tmp')
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            temp_file.replace(output_file)  # Atomic rename
        logger.info(f"Saved {unique_count} total contracts to {output_file} in {elapsed_time:.2f}s")
        return output_file, unique_count
    except Exception as e:
        logger.error(f"Error saving contracts to file: {e}", exc_info=True)
        raise


@app.post("/admin/clone_database")
async def clone_database(
        total: int = Query(-1, ge=-1),
        stages: Optional[Stage] = Query(None),
        updatedFrom: Optional[str] = Query(None, max_length=19),
        updatedTo: Optional[str] = Query(None, max_length=19),
        background: bool = Query(False),
):
    """Clone tender contract dataset from Find a Tender API into a local JSON file.

    Smart deduplication: Uses content hashing (SHA256) to detect true duplicates.

    Params:
      - total: Max contracts to fetch (-1 = unlimited)
      - stages: Optional filter (planning|tender|award)
      - updatedFrom/updatedTo: Optional date range
      - background: If True, return immediately and run in background (requires monitoring)

    Returns:
      - Status, file_path, summary stats, operation_id (if background=True)
    """

    # Security: Prevent excessive concurrent background operations
    active_bg_ops = sum(1 for op in CLONE_OPERATIONS.values() if not op.get("completed"))
    if background and active_bg_ops >= 2:
        logger.warning(f"Rejecting background clone: {active_bg_ops} already running")
        raise HTTPException(
            status_code=429,
            detail=f"Too many background operations running ({active_bg_ops}). Max: 2"
        )

    # Generate operation ID for tracking
    operation_id = f"clone_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{hash(str(stages) + str(total)) % 10000}"

    # Register operation
    CLONE_OPERATIONS[operation_id] = {
        "started_at": datetime.utcnow(),
        "total_requested": total,
        "stages": stages,
        "status": "running",
        "completed": False,
        "error": None,
    }

    logger.info(f"[{operation_id}] Starting clone_database with total={total}, stages={stages}, background={background}")

    # If background mode, return immediately and run async
    if background:
        # Create task and add done callback to handle exceptions
        task = asyncio.create_task(_clone_database_impl(operation_id, total, stages, updatedFrom, updatedTo))
        task.add_done_callback(lambda t: _handle_background_task_result(operation_id, t))
        return {
            "status": "background_queued",
            "operation_id": operation_id,
            "message": "Clone operation queued. Check status with /admin/clone_status/{operation_id}"
        }

    # Otherwise, run synchronously
    return await _clone_database_impl(operation_id, total, stages, updatedFrom, updatedTo)

def _handle_background_task_result(operation_id: str, task):
    """Callback to handle background task completion/failure."""
    try:
        # This will re-raise any exception from the task
        task.result()
    except Exception as e:
        logger.error(f"[{operation_id}] Background task failed: {str(e)}", exc_info=True)
        # Update operation status with actual error
        if operation_id in CLONE_OPERATIONS:
            CLONE_OPERATIONS[operation_id].update({
                "status": "failed",
                "completed": True,
                "error": str(e)
            })

async def _clone_database_impl(operation_id: str, total: int, stages: Optional[Stage],
                               updatedFrom: Optional[str], updatedTo: Optional[str]):
    """
    Internal implementation of clone_database.
    Can run in the foreground (awaited) or background (async task).
    """
    start_time = datetime.utcnow()
    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages"

    try:
        # Load existing contracts
        contracts_dict, existing_hashes = _load_existing_contracts()
        existing_count = len(contracts_dict)

        remaining = total if total > 0 else float('inf')
        api_limit = 100
        cursor: Optional[str] = None
        received_total = 0
        pages = 0
        newly_added = 0
        duplicates_skipped = 0

        # Freeze pagination parameters
        fixed_updated_to: Optional[str] = updatedTo
        fixed_updated_from: Optional[str] = updatedFrom
        fixed_stages: Optional[Stage] = stages

        logger.info(f"[{operation_id}] Existing contracts: {existing_count}")
        logger.info(f"[{operation_id}] Requested total: {total}")

        processed_total = 0

        while True:  # CHANGE: Use explicit break conditions instead of "while remaining > 0"
            # Add timeout protection for background runs
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed > 3600:  # 1 hour timeout
                logger.warning(f"[{operation_id}] Timeout reached (3600s). Stopping.")
                raise TimeoutError("Clone operation exceeded 1 hour maximum duration")

            # Check if we've reached the limit (for non-unlimited mode)
            if remaining != float('inf') and remaining <= 0:
                logger.info(f"[{operation_id}] Reached limit ({total} contracts). Stopping.")
                break

            params = {"limit": api_limit}
            if cursor:
                params["cursor"] = cursor
            if fixed_stages:
                params["stages"] = fixed_stages
            if fixed_updated_from:
                params["updatedFrom"] = fixed_updated_from
            if fixed_updated_to:
                params["updatedTo"] = fixed_updated_to

            logger.debug(f"[{operation_id}] Fetching page {pages + 1}, remaining={remaining}")

            try:
                data = await get_json_with_retry(url, params)
            except HTTPException as e:
                logger.error(f"[{operation_id}] API error: {e.detail}")
                raise

            # Freeze updatedTo on first request
            if fixed_updated_to is None:
                fixed_updated_to = _extract_query_param(data.get("uri"), "updatedTo")
                if fixed_updated_to is None:
                    fixed_updated_to = _extract_query_param(_safe_get(data, "links", "next"), "updatedTo")
                logger.info(f"[{operation_id}] Fixed updatedTo={fixed_updated_to}")

            releases = data.get("releases", []) or []

            if not releases:
                logger.info(f"[{operation_id}] No more releases from API. Stopping.")
                break

            pages += 1
            received_total += len(releases)

            # Determine how many to process from this page
            if remaining == float('inf'):
                page_releases = releases
            else:
                # Only take what we need
                items_needed = int(remaining)
                page_releases = releases[:items_needed]

            logger.debug(f"[{operation_id}] Processing {len(page_releases)} items from page {pages}")

            for rel in page_releases:
                ocid = rel.get("ocid")
                if not ocid:
                    continue

                content_hash = _hash_source(rel)

                if content_hash in existing_hashes:
                    duplicates_skipped += 1
                    logger.debug(f"[{operation_id}] Duplicate: {ocid}")
                else:
                    contracts_dict[ocid] = rel
                    existing_hashes.add(content_hash)
                    newly_added += 1

            # Update remaining count
            if remaining != float('inf'):
                processed_total += len(page_releases)
                remaining = total - processed_total
                logger.debug(
                    f"[{operation_id}] Processed {len(page_releases)} items. Total so far: {processed_total}/{total}, remaining: {remaining}")

            # Check again if we've reached the limit
            if remaining != float('inf') and remaining <= 0:
                logger.info(f"[{operation_id}] Reached limit ({total} contracts). Stopping.")
                break

            # Try to get next cursor
            cursor = data.get("nextCursor")
            if not cursor:
                next_url = _safe_get(data, "links", "next")
                cursor = _extract_cursor(next_url)

            if not cursor:
                logger.info(f"[{operation_id}] No more cursors available. Stopping.")
                break

        # Save to file
        output_file, total_unique = _save_contracts_to_file(
            contracts_dict, total, received_total, pages, duplicates_skipped, start_time
        )

        result = {
            "status": "success",
            "operation_id": operation_id,
            "file_path": str(output_file),
            "total_contracts_in_file": total_unique,
            "newly_added": newly_added,
            "duplicates_skipped": duplicates_skipped,
            "requested": total if total > 0 else "unlimited",
            "received_raw": received_total,
            "pages": pages,
            "elapsed_seconds": round((datetime.utcnow() - start_time).total_seconds(), 2),
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        logger.info(f"[{operation_id}] SUCCESS: {newly_added} added, {duplicates_skipped} skipped")

        # Update operation status
        CLONE_OPERATIONS[operation_id].update({
            "status": "completed",
            "completed": True,
            "result": result
        })

        return result


    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"[{operation_id}] ERROR: {error_msg}", exc_info=True)
        CLONE_OPERATIONS[operation_id].update({
            "status": "failed",
            "completed": True,
            "error": error_msg
        })

        raise HTTPException(status_code=500, detail=f"Clone failed: {error_msg}")

@app.get("/admin/clone_status/{operation_id}")
def clone_status(operation_id: str):
    """Check the status of a background clone operation."""
    if operation_id not in CLONE_OPERATIONS:
        raise HTTPException(status_code=404, detail="Operation not found")

    op = CLONE_OPERATIONS[operation_id]
    return {
        "operation_id": operation_id,
        "status": op["status"],
        "started_at": op["started_at"].isoformat(),
        "elapsed_seconds": (datetime.utcnow() - op["started_at"]).total_seconds(),
        "result": op.get("result"),
        "error": op.get("error"),
    }