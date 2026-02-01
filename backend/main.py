import asyncio
import asyncpg
import hashlib
import httpx
import os
import logging
import json
import random
import math
from datetime import datetime
from typing import Optional, Literal, Any, Dict, List
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

FIND_TENDER_BASE_URL = os.getenv("FIND_TENDER_BASE_URL", "https://www.find-tender.service.gov.uk")
FIND_TENDER_VERSION = os.getenv("FIND_TENDER_VERSION", "1.0")
DATABASE_URL = os.getenv("DATABASE_URL")

Stage = Literal["planning", "tender", "award"]

logger = logging.getLogger("app")

HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0, pool=60.0)
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=10, max_connections=20)
HTTP_RETRY_STATUS = {429, 502, 503, 504}
HTTP_MAX_ATTEMPTS = 6
HTTP_BACKOFF_CAP = 30

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


def _compute_backoff(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after and retry_after.isdigit():
        return max(1, min(float(retry_after), HTTP_BACKOFF_CAP))
    return min((2 ** attempt), HTTP_BACKOFF_CAP)


async def _sleep_with_jitter(seconds: float) -> None:
    await asyncio.sleep(seconds + random.uniform(0, 0.5))


async def get_json_with_retry(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS) as client:
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

                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as e:
                    raise HTTPException(status_code=502, detail=f"FindTender error {r.status_code}: {r.text}") from e

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

    raise HTTPException(status_code=502, detail="FindTender: exceeded retries")


def _require_db_pool():
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured (PostgreSQL required)")
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


def _sanitize_json(obj: Any) -> Any:
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return None
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    return obj


def _build_full_text(release: dict) -> str:
    parts = []
    ocid = release.get("ocid")
    if ocid:
        parts.append(ocid)

    tender = release.get("tender", {})
    parts.extend([tender.get("title"), tender.get("description"), tender.get("mainProcurementCategory")])

    classification = tender.get("classification", {}) or {}
    parts.extend([classification.get("id"), classification.get("description")])

    buyer = release.get("buyer", {})
    parts.append(buyer.get("name"))

    items = tender.get("items", []) or []
    for it in items:
        for c in it.get("additionalClassifications", []) or []:
            if c.get("id"):
                parts.append(c.get("id"))
            if c.get("description"):
                parts.append(c.get("description"))

    for lot in tender.get("lots", []) or []:
        parts.append(lot.get("title"))
        parts.append(lot.get("description"))

    return "\n".join([p for p in parts if p])


def _extract_cpv_list(release: dict) -> List[str]:
    cpv_values: List[str] = []
    tender = release.get("tender", {}) or {}

    classification = tender.get("classification", {}) or {}
    classification_id = classification.get("id")
    if classification_id:
        cpv_values.append(str(classification_id))

    additional_classifications = tender.get("additionalClassifications", []) or []
    for c in additional_classifications:
        cid = c.get("id")
        if cid:
            cpv_values.append(str(cid))

    items = tender.get("items", []) or []
    for it in items:
        for c in it.get("additionalClassifications", []) or []:
            cid = c.get("id")
            if cid:
                cpv_values.append(str(cid))

    if not cpv_values:
        return []

    # Keep order but remove duplicates
    seen = set()
    deduped = []
    for cpv in cpv_values:
        if cpv in seen:
            continue
        seen.add(cpv)
        deduped.append(cpv)
    return deduped


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
    """Search in local PostgreSQL table `tenders`."""
    pool = _require_db_pool()

    async with pool.acquire() as conn:
        if mode == "exact":
            sql = """
                SELECT ocid, title, description, published_at, url,
                    ts_rank_cd(
                        coalesce(search_tsv, to_tsvector('english'::regconfig, coalesce(full_text, ''))),
                        phraseto_tsquery('english'::regconfig, $1)
                    ) AS score
                FROM tenders
                WHERE coalesce(search_tsv, to_tsvector('english'::regconfig, coalesce(full_text, '')))
                    @@ phraseto_tsquery('english'::regconfig, $1)
                ORDER BY score DESC, published_at DESC NULLS LAST
                LIMIT $2;
            """
            rows = await conn.fetch(sql, q, limit)
        else:
            sql = """
                SELECT ocid, title, description, published_at, url,
                    GREATEST(
                        ts_rank_cd(
                            coalesce(search_tsv, to_tsvector('english'::regconfig, coalesce(full_text, ''))),
                            websearch_to_tsquery('english'::regconfig, $1)
                        ),
                        similarity(coalesce(full_text, ''), $1)
                    ) AS score
                FROM tenders
                WHERE coalesce(search_tsv, to_tsvector('english'::regconfig, coalesce(full_text, '')))
                    @@ websearch_to_tsquery('english'::regconfig, $1)
                    OR similarity(coalesce(full_text, ''), $1) > 0.20
                ORDER BY score DESC, published_at DESC NULLS LAST
                LIMIT $2;
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
        updatedFrom: Optional[str] = Query(None, max_length=19),
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
    return await get_json_with_retry(url, params)


@app.get("/tenders/{notice_or_ocid}")
async def get_tender(notice_or_ocid: str):
    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages/{notice_or_ocid}"
    return await get_json_with_retry(url, {})


@app.post("/admin/ingest/first")
async def ingest_first_notices(limit: int = Query(20, ge=1, le=100)):
    pool = _require_db_pool()

    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages"
    data = await get_json_with_retry(url, {"limit": limit})
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
                cpv = _extract_cpv_list(rel)
                source_hash = _hash_source(rel)
                rel_json = json.dumps(_sanitize_json(rel), ensure_ascii=False, allow_nan=False)

                sql = """
                    INSERT INTO tenders (
                        ocid, title, description, full_text, published_at, url, source_hash, cpv, data, search_tsv
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8::text[], $9::jsonb,
                        setweight(to_tsvector('english'::regconfig, coalesce($2, '')), 'A') ||
                        setweight(to_tsvector('english'::regconfig, coalesce($3, '')), 'B') ||
                        setweight(to_tsvector('english'::regconfig, coalesce(array_to_string($8::text[], ' '), '')), 'B') ||
                        setweight(to_tsvector('english'::regconfig, coalesce($4, '')), 'C')
                    )
                    ON CONFLICT (ocid) DO UPDATE SET
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        full_text = EXCLUDED.full_text,
                        published_at = EXCLUDED.published_at,
                        url = EXCLUDED.url,
                        source_hash = EXCLUDED.source_hash,
                        cpv = EXCLUDED.cpv,
                        data = EXCLUDED.data,
                        search_tsv = EXCLUDED.search_tsv,
                        updated_at = NOW()
                    RETURNING (xmax = 0) AS inserted;
                """

                row = await conn.fetchrow(
                    sql, ocid, title, description, full_text, published_at, url_doc, source_hash, cpv, rel_json
                )
                if row and row["inserted"]:
                    inserted += 1
                else:
                    updated += 1

    return {"requested": limit, "received": len(releases), "inserted": inserted, "updated": updated}


@app.post("/admin/ingest")
async def ingest_notices(
        total: int = Query(200, ge=1, le=5000),
        stages: Optional[Stage] = Query(None),
        updatedFrom: Optional[str] = Query(None, max_length=19),
        updatedTo: Optional[str] = Query(None, max_length=19),
):
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
            fixed_updated_to: Optional[str] = updatedTo
            fixed_updated_from: Optional[str] = updatedFrom
            fixed_stages: Optional[Stage] = stages

            while remaining > 0:
                params: Dict[str, Any] = {"limit": api_limit}
                if cursor:
                    params["cursor"] = cursor
                if fixed_stages:
                    params["stages"] = fixed_stages
                if fixed_updated_from:
                    params["updatedFrom"] = fixed_updated_from
                if fixed_updated_to:
                    params["updatedTo"] = fixed_updated_to

                data = await get_json_with_retry(url, params)

                if fixed_updated_to is None:
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
                    cpv = _extract_cpv_list(rel)
                    source_hash = _hash_source(rel)
                    rel_json = json.dumps(_sanitize_json(rel), ensure_ascii=False, allow_nan=False)

                    sql = """
                        INSERT INTO tenders (
                            ocid, title, description, full_text, published_at, url, source_hash, cpv, data, search_tsv
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8::text[], $9::jsonb,
                            setweight(to_tsvector('english'::regconfig, coalesce($2, '')), 'A') ||
                            setweight(to_tsvector('english'::regconfig, coalesce($3, '')), 'B') ||
                            setweight(to_tsvector('english'::regconfig, coalesce(array_to_string($8::text[], ' '), '')), 'B') ||
                            setweight(to_tsvector('english'::regconfig, coalesce($4, '')), 'C')
                        )
                        ON CONFLICT (ocid) DO UPDATE SET
                            title = EXCLUDED.title,
                            description = EXCLUDED.description,
                            full_text = EXCLUDED.full_text,
                            published_at = EXCLUDED.published_at,
                            url = EXCLUDED.url,
                            source_hash = EXCLUDED.source_hash,
                            cpv = EXCLUDED.cpv,
                            data = EXCLUDED.data,
                            search_tsv = EXCLUDED.search_tsv,
                            updated_at = NOW()
                        RETURNING (xmax = 0) AS inserted;
                    """

                    row = await conn.fetchrow(
                        sql, ocid, title, description, full_text, published_at, url_doc, source_hash, cpv, rel_json
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
                    cursor = _extract_cursor(_safe_get(data, "links", "next"))
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


# ---------------------------
# Clone subsystem (moved to clone.py)
# ---------------------------
try:
    from .clone import register_clone_routes
except ImportError:
    from clone import register_clone_routes

register_clone_routes(
    app,
    base_url=FIND_TENDER_BASE_URL,
    api_version=FIND_TENDER_VERSION,
    stage_type=Stage,
    safe_get=_safe_get,
    extract_query_param=_extract_query_param,
    extract_cursor=_extract_cursor,
)
