from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
import requests
from pydantic import BaseModel

app = FastAPI(title="Tender Search API")

# Configure CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchFilter(BaseModel):
    field: str
    value: str


class SearchRequest(BaseModel):
    keywords: Optional[str] = None
    filters: Optional[List[SearchFilter]] = []
    filter_operator: str = "AND"  # "AND" or "OR"


@app.get("/")
async def root():
    return {"message": "Tender Search API", "status": "running"}


@app.post("/search")
async def search_tenders(search_request: SearchRequest):
    """
    Search for tenders from find-tender.service.gov.uk
    
    Args:
        search_request: Contains keywords, filters, and filter_operator (AND/OR)
    
    Returns:
        Search results from the tender service
    """
    base_url = "https://www.find-tender.service.gov.uk/Search"
    
    # Build query parameters
    params = {}
    
    # Add keywords if provided
    if search_request.keywords:
        params["keywords"] = search_request.keywords
    
    # Apply filters based on operator
    if search_request.filters:
        if search_request.filter_operator.upper() == "AND":
            # For AND operator, all filters must match
            for f in search_request.filters:
                params[f.field] = f.value
        elif search_request.filter_operator.upper() == "OR":
            # For OR operator, combine filter values with OR logic
            # Note: The actual implementation depends on the API's support for OR operations
            # This is a simplified version that sends all filters
            for f in search_request.filters:
                if f.field in params:
                    # If field already exists, append with OR logic
                    params[f.field] = f"{params[f.field]}|{f.value}"
                else:
                    params[f.field] = f.value
    
    try:
        # Make request to tender service
        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        
        # Return the response
        # Note: Adjust based on actual API response format
        return {
            "status": "success",
            "query": params,
            "filter_operator": search_request.filter_operator,
            "url": response.url,
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            # The actual tender service returns HTML, so we'll return metadata
            "message": "Search completed successfully. In production, parse HTML or use proper API."
        }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "message": str(e),
            "query": params
        }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
