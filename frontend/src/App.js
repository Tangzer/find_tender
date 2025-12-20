import React, { useState } from 'react';
import axios from 'axios';
import './App.css';

function App() {
  const [keywords, setKeywords] = useState('');
  const [filters, setFilters] = useState([{ field: '', value: '' }]);
  const [filterOperator, setFilterOperator] = useState('AND');
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleAddFilter = () => {
    setFilters([...filters, { field: '', value: '' }]);
  };

  const handleRemoveFilter = (index) => {
    const newFilters = filters.filter((_, i) => i !== index);
    setFilters(newFilters.length === 0 ? [{ field: '', value: '' }] : newFilters);
  };

  const handleFilterChange = (index, key, value) => {
    const newFilters = [...filters];
    newFilters[index][key] = value;
    setFilters(newFilters);
  };

  const handleSearch = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResults(null);

    try {
      // Filter out empty filters
      const validFilters = filters.filter(f => f.field && f.value);

      const response = await axios.post('http://localhost:8000/search', {
        keywords: keywords || null,
        filters: validFilters,
        filter_operator: filterOperator
      });

      setResults(response.data);
    } catch (err) {
      setError(err.message || 'An error occurred while searching');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="App">
      <header className="App-header">
        <h1>🔍 Find Tender Search</h1>
        <p>Search UK government procurement opportunities</p>
      </header>

      <main className="App-main">
        <form onSubmit={handleSearch} className="search-form">
          <div className="form-group">
            <label htmlFor="keywords">Keywords:</label>
            <input
              id="keywords"
              type="text"
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              placeholder="Enter search keywords..."
              className="form-control"
            />
          </div>

          <div className="filters-section">
            <div className="filters-header">
              <h3>Filters</h3>
              <div className="operator-toggle">
                <label>
                  <input
                    type="radio"
                    value="AND"
                    checked={filterOperator === 'AND'}
                    onChange={(e) => setFilterOperator(e.target.value)}
                  />
                  <span className="operator-label">AND (All must match)</span>
                </label>
                <label>
                  <input
                    type="radio"
                    value="OR"
                    checked={filterOperator === 'OR'}
                    onChange={(e) => setFilterOperator(e.target.value)}
                  />
                  <span className="operator-label">OR (Any can match)</span>
                </label>
              </div>
            </div>

            {filters.map((filter, index) => (
              <div key={index} className="filter-row">
                <input
                  type="text"
                  value={filter.field}
                  onChange={(e) => handleFilterChange(index, 'field', e.target.value)}
                  placeholder="Field (e.g., status, region)"
                  className="form-control filter-field"
                />
                <input
                  type="text"
                  value={filter.value}
                  onChange={(e) => handleFilterChange(index, 'value', e.target.value)}
                  placeholder="Value"
                  className="form-control filter-value"
                />
                <button
                  type="button"
                  onClick={() => handleRemoveFilter(index)}
                  className="btn btn-remove"
                  disabled={filters.length === 1}
                >
                  Remove
                </button>
              </div>
            ))}

            <button
              type="button"
              onClick={handleAddFilter}
              className="btn btn-add"
            >
              + Add Filter
            </button>
          </div>

          <button type="submit" className="btn btn-search" disabled={loading}>
            {loading ? 'Searching...' : 'Search Tenders'}
          </button>
        </form>

        {error && (
          <div className="alert alert-error">
            <strong>Error:</strong> {error}
          </div>
        )}

        {results && (
          <div className="results">
            <h2>Search Results</h2>
            <div className="result-card">
              <div className="result-item">
                <strong>Status:</strong> {results.status}
              </div>
              {results.message && (
                <div className="result-item">
                  <strong>Message:</strong> {results.message}
                </div>
              )}
              <div className="result-item">
                <strong>Filter Operator:</strong> {results.filter_operator}
              </div>
              {results.query && (
                <div className="result-item">
                  <strong>Query Parameters:</strong>
                  <pre>{JSON.stringify(results.query, null, 2)}</pre>
                </div>
              )}
              {results.url && (
                <div className="result-item">
                  <strong>Request URL:</strong>
                  <a href={results.url} target="_blank" rel="noopener noreferrer">
                    {results.url}
                  </a>
                </div>
              )}
            </div>
          </div>
        )}
      </main>

      <footer className="App-footer">
        <p>Data from <a href="https://www.find-tender.service.gov.uk" target="_blank" rel="noopener noreferrer">find-tender.service.gov.uk</a></p>
      </footer>
    </div>
  );
}

export default App;
