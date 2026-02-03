# Find Tender - Search Application

A web application for searching UK government procurement opportunities from [find-tender.service.gov.uk](https://www.find-tender.service.gov.uk) with phrase-focused search.

## Features

- 🔍 **Phrase Search**: Exact and near-phrase search
- ⚡ **FastAPI Backend**: Async Python backend
- 🧠 **PostgreSQL Search**: Full-text + trigram similarity
- 📦 **Raw Clone**: Optional API raw clone for offline analysis

## Project Structure

```
find_tender/
├── backend/            # FastAPI backend application
│   ├── main.py
│   ├── clone.py
│   ├── requirements.txt
│   └── .env
├── frontend/           # React frontend application
│   ├── public/
│   │   └── index.html
│   ├── src/
│   │   ├── App.js
│   │   ├── App.css
│   │   ├── index.js
│   │   └── index.css
│   └── package.json
├── data/               # Local data dumps / clone artifacts
├── logs/               # Runtime logs
└── README.md
```

## Quick Commands (Backend)

Start API (from repo root):
```bash
uvicorn backend.main:app --reload --port 8000
```

Start API (from `backend/`):
```bash
uvicorn main:app --reload --port 8000
```

Health check:
```bash
curl -sS http://127.0.0.1:8000/health
```

## Prerequisites

- Python 3.8 or higher
- Node.js 14 or higher
- npm or yarn

## Installation

### Backend Setup

0. Start virtualenv (optional)
```bash
python -m venv .venv
source .venv/bin/activate
```

1. Install Python dependencies:
```bash
pip install -r backend/requirements.txt
```

### Frontend Setup

1. Navigate to the frontend directory:
```bash
cd frontend
```

2. Install Node dependencies:
```bash
npm install
```

## Running the Application

### Start the Backend Server

From the root directory:
```bash
uvicorn backend.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`

API Endpoints (core):
- `GET /` - API info
- `GET /health` - Health check
- `GET /search` - Search (phrase-based)
- `GET /tenders` - Proxy Find a Tender list
- `GET /tenders/{notice_or_ocid}` - Proxy Find a Tender detail
- `POST /admin/ingest` - Ingest from Find a Tender API into Postgres
- `POST /admin/ingest/first` - Ingest first page
- `POST /admin/clone_database` - Raw API clone to disk
- `GET /admin/clone_status/{operation_id}` - Clone status

### Start the Frontend Development Server

In a new terminal, from the frontend directory:
```bash
npm start
```

The React app will open at `http://localhost:3000`

## Usage

### API Search (phrase-based)

Exact phrase:
```bash
curl -sS "http://127.0.0.1:8000/search?q=24%20hour%20care%20and%20support%20service&mode=exact&limit=10"
```

Near search:
```bash
curl -sS "http://127.0.0.1:8000/search?q=24%20hour%20care%20and%20support%20service&mode=near&limit=10"
```

Pretty JSON output:
```bash
curl -sS "http://127.0.0.1:8000/search?q=24%20hour%20care%20and%20support%20service&mode=near&limit=10" | python -m json.tool
```

Or using `jq`:
```bash
curl -sS "http://127.0.0.1:8000/search?q=24%20hour%20care%20and%20support%20service&mode=near&limit=10" | jq
```

Select fields only:
```bash
curl -sS "http://127.0.0.1:8000/search?q=24%20hour%20care%20and%20support%20service&mode=near&limit=10" \
  | jq '.[] | {ocid, title, published_at, score}'
```

### Ingest from Find a Tender API

First page (20 by default):
```bash
curl -sS -X POST "http://127.0.0.1:8000/admin/ingest/first"
```

Commit per page (default):
```bash
curl -sS -X POST "http://127.0.0.1:8000/admin/ingest?total=2000&commit_every=1"
```

N pages (API is 100/page, so 20 pages ≈ 2000):
```bash
curl -sS -X POST "http://127.0.0.1:8000/admin/ingest?total=2000"
```

Batch commits every 5 pages:
```bash
curl -sS -X POST "http://127.0.0.1:8000/admin/ingest?total=2000&commit_every=5"
```

## Clone Database Endpoint

### Overview

The `/admin/clone_database` endpoint fetches tender contracts from the Find a Tender API and writes a raw, append-only clone under `data/clones/<operation_id>/`. It uses content-based deduplication (SHA256 hashing) to prevent duplicates while preserving the exact API payloads. This raw clone is the authoritative mirror of the upstream API and is later used to build an optimized copy.

### Endpoint: `POST /admin/clone_database`

Parameters:
- `total` (int, default: `-1`): Max contracts to fetch. Use `-1` to fetch all available data (unlimited).
- `stages` (optional): Filter by stage (`planning`, `tender`, or `award`)
- `updatedFrom` (optional): Start date filter (`YYYY-MM-DDTHH:MM:SS`)
- `updatedTo` (optional): End date filter (`YYYY-MM-DDTHH:MM:SS`)
- `background` (bool, default: `false`): Run in background and return immediately

Usage examples:

Test with 5 contracts (synchronous):
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=5"
```

Fetch all available contracts (unlimited):
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=-1"
```

Fetch 100 planning stage contracts (synchronous):
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=5"
```

Run in background (returns immediately, monitor status separately):
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=-1&background=true"
```

Check background operation status:
```bash
curl "http://localhost:8000/admin/clone_status/{operation_id}"
```

### Clone Output Structure (Raw Mirror)

Each clone creates a self-contained folder at `data/clones/<operation_id>/`:

- `objects/sha256/*.json.gz`: Gzipped raw JSON payloads, keyed by SHA256 of the canonical JSON bytes. This is the exact upstream data, stored once per unique payload.
- `events/part-000001.ndjson` (and subsequent parts): Line-delimited JSON events for every fetched release. Each event records `fetched_at`, `cursor`, `page`, `ocid`, `content_hash`, and upstream version metadata. Parts rotate at ~100MB to keep files manageable.
- `checkpoint.json`: Resume state for long-running jobs (cursor, page counts, counters, fixed filters). Used to continue a clone safely after interruption.
- `status.json`: The latest runtime status or final result for the operation (progress, errors, or completion).
- `manifest.json`: Final summary describing parameters, stats, timing, and layout.

Important rule: treat the raw clone as immutable. Any normalization, indexing, or enrichment must happen in a separate optimized copy so the raw store continues to match the upstream API.

## Development

### Backend Development

The FastAPI backend is in `backend/main.py`. To add new features:
1. Edit `backend/main.py`
2. Restart the server with `uvicorn backend.main:app --reload --port 8000`

The API includes auto-generated documentation at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Frontend Development

The React frontend is in the `frontend/src` directory:
- `App.js` - Main component
- `App.css` - Styling for the application

Changes will hot-reload automatically when running `npm start`.

## Technologies Used

### Backend
- **FastAPI**: Modern, fast web framework for building APIs
- **Uvicorn**: ASGI server for Python
- **httpx**: HTTP client for calling the tender service
- **Pydantic**: Data validation using Python type annotations

### Frontend
- **React**: JavaScript library for building user interfaces
- **Axios**: Promise-based HTTP client
- **CSS3**: Modern styling with flexbox and gradients
