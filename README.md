# Find Tender - Search Application

A web application for searching UK government procurement opportunities from [find-tender.service.gov.uk](https://www.find-tender.service.gov.uk) with advanced AND/OR filter capabilities.

## Features

- 🔍 **Keyword Search**: Search tenders by keywords
- 🎯 **Advanced Filters**: Add multiple filters with custom fields and values
- 🔀 **AND/OR Logic**: Toggle between AND (all filters must match) and OR (any filter can match) operators
- 🎨 **Modern UI**: Clean, responsive React interface
- ⚡ **FastAPI Backend**: Fast and efficient Python backend with async support

## Project Structure

```
find_tender/
├── main.py              # FastAPI backend application
├── requirements.txt     # Python dependencies
├── frontend/           # React frontend application
│   ├── public/
│   │   └── index.html
│   ├── src/
│   │   ├── App.js      # Main React component with filter UI
│   │   ├── App.css     # Application styles
│   │   ├── index.js    # React entry point
│   │   └── index.css   # Global styles
│   └── package.json    # Node dependencies
└── README.md
```

#### SHORTCUTS
### Start the Backend Server
From the root directory:
```bash
uvicorn backend.main:app --reload --port 8000
```
**Run in background (returns immediately, monitor status separately):**
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=-1&background=true"
```
**Check background operation status:**
```bash
curl "http://localhost:8000/admin/clone_status/{operation_id}"
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

2. Install Python dependencies:
```bash
pip install -r requirements.txt
```
or
```bash
python -m pip install -r requirements.txt
```## Frontend Setup

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

API Endpoints:
- `GET /` - API information
- `POST /search` - Search tenders with filters
- `GET /health` - Health check

### Start the Frontend Development Server

In a new terminal, from the frontend directory:
```bash
npm start
```

The React app will open at `http://localhost:3000`

## Usage

### Web Interface

1. Open `http://localhost:3000` in your browser
2. Enter search keywords (optional)
3. Add filters by clicking "+ Add Filter"
4. Enter field names (e.g., "status", "region") and values for each filter
5. Choose between AND or OR filter operators:
   - **AND**: All filters must match (more restrictive)
   - **OR**: Any filter can match (more inclusive)
6. Click "Search Tenders" to execute the search

### API Usage

You can also use the API directly:

```bash
# Search with AND filters
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "keywords": "software development",
    "filters": [
      {"field": "status", "value": "open"},
      {"field": "region", "value": "London"}
    ],
    "filter_operator": "AND"
  }'

# Search with OR filters
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "keywords": "consulting",
    "filters": [
      {"field": "region", "value": "London"},
      {"field": "region", "value": "Manchester"}
    ],
    "filter_operator": "OR"
  }'
```
## Clone Database Endpoint

### Overview

The `/admin/clone_database` endpoint fetches tender contracts from the Find a Tender API and writes a raw, append-only clone under `data/clones/<operation_id>/`. It uses **content-based deduplication** (SHA256 hashing) to prevent duplicates while preserving the exact API payloads. This raw clone is the authoritative mirror of the upstream API and is later used to build an optimized copy.

### Endpoint: `POST /admin/clone_database`

**Parameters:**
- `total` (int, default: `-1`): Max contracts to fetch. Use `-1` to fetch all available data (unlimited).
- `stages` (optional): Filter by stage (`planning`, `tender`, or `award`)
- `updatedFrom` (optional): Start date filter (`YYYY-MM-DDTHH:MM:SS`)
- `updatedTo` (optional): End date filter (`YYYY-MM-DDTHH:MM:SS`)
- `background` (bool, default: `false`): Run in background and return immediately

### Usage Examples

**Test with 5 contracts (synchronous):**
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=5"
```

**Fetch all available contracts (unlimited):**
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=-1"
```

**Fetch 100 planning stage contracts (synchronous):**
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=5"
```

**Run in background (returns immediately, monitor status separately):**
```bash
curl -X POST "http://localhost:8000/admin/clone_database?total=-1&background=true"
```

**Check background operation status:**
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

Why this structure:
- **Exact upstream mirror**: Raw objects are stored unmodified, so the clone always reflects the API database.
- **Dedup without loss**: The content hash prevents duplicates while keeping every fetch recorded via events.
- **Auditable & replayable**: Events provide a full fetch log and let us rebuild or validate an optimized copy deterministically.
- **Resumable**: Checkpoints allow safe restarts for large data pulls.

Important rule: treat the raw clone as immutable. Any normalization, indexing, or enrichment must happen in a separate optimized copy so the raw store continues to match the upstream API.

## Filter Logic

### AND Operator
When using AND, all specified filters must match. For example:
- Filter 1: `status = "open"`
- Filter 2: `region = "London"`

Result: Only tenders that are BOTH open AND in London

### OR Operator
When using OR, any of the specified filters can match. For example:
- Filter 1: `region = "London"`
- Filter 2: `region = "Manchester"`

Result: Tenders in London OR Manchester (or both)

## Development

### Backend Development

The FastAPI backend is in `main.py`. To add new features:
1. Edit `main.py`
2. Restart the server with `python main.py`

The API includes auto-generated documentation at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Frontend Development

The React frontend is in the `frontend/src` directory:
- `App.js` - Main component with search form and filter logic
- `App.css` - Styling for the application

Changes will hot-reload automatically when running `npm start`.

## Technologies Used

### Backend
- **FastAPI**: Modern, fast web framework for building APIs
- **Uvicorn**: ASGI server for Python
- **Requests**: HTTP library for calling the tender service
- **Pydantic**: Data validation using Python type annotations

### Frontend
- **React**: JavaScript library for building user interfaces
- **Axios**: Promise-based HTTP client
- **CSS3**: Modern styling with flexbox and gradients
