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

## License

This project is open source and available under the MIT License.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
