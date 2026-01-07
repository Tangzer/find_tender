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

@app.get("/")
def base():
    return {"Working ?" : "Yes"}


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
    limit: int = Query(50, ge=1, le=100),
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

#     def _count_items(page: Dict[str, Any]) -> int:
#         for key in ("releases", "results", "items", "ocdsReleasePackages"):
#             v = page.get(key)
#             if isinstance(v, list):
#                 return len(v)
#         return 0
# #
#     pages_fetched = 0
#     total_items = 0
#     next_url = url
#     current_params = params
#
#     for _ in range(1):
#         data = await get_json_with_retry(next_url, current_params)
#         pages_fetched += 1
#         total_items += _count_items(data)
#
#         links = data.get("links", {})
#         next_url = links.get("next")
#         # Après la première requête, les calls suivants suivent l'URL `next` complète,
#         # donc on n'envoie pas à nouveau les params initiaux.
#         current_params = {}
#
#     return {"pages_fetched": pages_fetched, "total_items": total_items}



@app.get("/tenders/{notice_or_ocid}")
async def get_tender(notice_or_ocid: str):
    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages/{notice_or_ocid}"
    data = await get_json_with_retry(url, {})
    return data
