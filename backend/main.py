import os
import time
from typing import Optional, Literal, Any, Dict

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import asyncio
import asyncpg
from pydantic import BaseModel
import hashlib
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
    # Respecte Retry-After en cas de 429/503
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(6):
            r = await client.get(url, params=params)

            if r.status_code in (429, 503):
                retry_after = r.headers.get("Retry-After")
                wait = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
                await asyncio.sleep(max(1, wait))
                continue

            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                # remonte l'erreur lisible côté frontend
                detail = r.text
                raise HTTPException(status_code=502, detail=f"FindTender error {r.status_code}: {detail}") from e

            return r.json()

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
            SELECT
              ocid,
              title,
              description,
              published_at,
              url,
              ts_rank_cd(to_tsvector('english', coalesce(full_text,'')), phraseto_tsquery('english', $1)) AS score
            FROM tenders
            WHERE to_tsvector('english', coalesce(full_text,'')) @@ phraseto_tsquery('english', $1)
            ORDER BY score DESC, published_at DESC NULLS LAST
            LIMIT $2;
            """
            rows = await conn.fetch(sql, q, limit)
        else:
            # near: combine full-text + trigram similarity (pg_trgm)
            sql = """
            SELECT
              ocid,
              title,
              description,
              published_at,
              url,
              GREATEST(
                ts_rank_cd(to_tsvector('english', coalesce(full_text,'')), websearch_to_tsquery('english', $1)),
                similarity(coalesce(full_text,''), $1)
              ) AS score
            FROM tenders
            WHERE
              to_tsvector('english', coalesce(full_text,'')) @@ websearch_to_tsquery('english', $1)
              OR similarity(coalesce(full_text,''), $1) > 0.20
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
    limit: int = Query(1, ge=1, le=100),
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
                INSERT INTO tenders (
                  ocid, title, description, full_text, published_at, url, source_hash
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7
                )
                ON CONFLICT (ocid)
                DO UPDATE SET
                  title = EXCLUDED.title,
                  description = EXCLUDED.description,
                  full_text = EXCLUDED.full_text,
                  published_at = EXCLUDED.published_at,
                  url = EXCLUDED.url,
                  source_hash = EXCLUDED.source_hash,
                  updated_at = NOW()
                RETURNING (xmax = 0) AS inserted;
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
                    # Find a Tender sets an implicit updatedTo when not provided.
                    # We MUST reuse that same updatedTo for all subsequent cursor requests.
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
                    INSERT INTO tenders (
                      ocid, title, description, full_text, published_at, url, source_hash
                    ) VALUES (
                      $1, $2, $3, $4, $5, $6, $7
                    )
                    ON CONFLICT (ocid)
                    DO UPDATE SET
                      title = EXCLUDED.title,
                      description = EXCLUDED.description,
                      full_text = EXCLUDED.full_text,
                      published_at = EXCLUDED.published_at,
                      url = EXCLUDED.url,
                      source_hash = EXCLUDED.source_hash,
                      updated_at = NOW()
                    RETURNING (xmax = 0) AS inserted;
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
