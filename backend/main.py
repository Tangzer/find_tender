import os
import time
from typing import Optional, Literal, Any, Dict

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

FIND_TENDER_BASE_URL = os.getenv("FIND_TENDER_BASE_URL", "https://www.find-tender.service.gov.uk")
FIND_TENDER_VERSION = os.getenv("FIND_TENDER_VERSION", "1.0")

Stage = Literal["planning", "tender", "award"]

app = FastAPI(title="Tender MVP API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def get_json_with_retry(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    # Respecte Retry-After en cas de 429/503
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(6):
            r = await client.get(url, params=params)

            if r.status_code in (429, 503):
                retry_after = r.headers.get("Retry-After")
                wait = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
                time.sleep(max(1, wait))
                continue

            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                # remonte l'erreur lisible côté frontend
                detail = r.text
                raise HTTPException(status_code=502, detail=f"FindTender error {r.status_code}: {detail}") from e

            return r.json()

    raise HTTPException(status_code=502, detail="FindTender: exceeded retries")


@app.get("/")
def base():
	return {"Working ?" : "Yes"}

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tenders")
async def list_tenders(
    limit: int = Query(20, ge=1, le=100),
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
    return data


@app.get("/tenders/{notice_or_ocid}")
async def get_tender(notice_or_ocid: str):
    url = f"{FIND_TENDER_BASE_URL}/api/{FIND_TENDER_VERSION}/ocdsReleasePackages/{notice_or_ocid}"
    data = await get_json_with_retry(url, {})
    return data
