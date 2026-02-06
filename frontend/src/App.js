import React, { useMemo, useState } from 'react';
import './App.css';

const STAGES = [
  { value: 'planning', label: 'Planning' },
  { value: 'tender', label: 'Tender' },
  { value: 'award', label: 'Award' },
];

const LIMIT_OPTIONS = [25, 50, 100];

function App() {
  const [keywords, setKeywords] = useState('');
  const [mode, setMode] = useState('exact');
  const [limit, setLimit] = useState(25);
  const [cpv, setCpv] = useState('');
  const [stages, setStages] = useState([]);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [lastQuery, setLastQuery] = useState(null);

  const stageSummary = useMemo(() => {
    if (stages.length === 0) return 'Any stage';
    return stages.map((stage) => STAGES.find((s) => s.value === stage)?.label).join(', ');
  }, [stages]);

  const handleStageToggle = (value) => {
    setStages((prev) => (prev.includes(value) ? prev.filter((v) => v !== value) : [...prev, value]));
  };

  const clearFilters = () => {
    setCpv('');
    setStages([]);
    setDateFrom('');
    setDateTo('');
  };

  const handleSearch = async (event) => {
    event.preventDefault();
    setError('');

    const trimmed = keywords.trim();
    if (trimmed.length < 2) {
      setError('Enter at least 2 characters for the keyword search.');
      return;
    }

    const params = new URLSearchParams();
    params.set('q', trimmed);
    params.set('mode', mode);
    params.set('limit', String(limit));
    if (dateFrom) params.set('updatedFrom', `${dateFrom}T00:00:00`);
    if (dateTo) params.set('updatedTo', `${dateTo}T23:59:59`);
    if (stages.length > 0) params.set('stages', stages.join(','));
    if (cpv.trim()) params.set('cpv', cpv.trim());

    setLoading(true);
    setResults([]);

    try {
      const response = await fetch(`/search?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Search failed (${response.status})`);
      }
      const data = await response.json();
      setResults(Array.isArray(data) ? data : []);
      setLastQuery({
        keywords: trimmed,
        mode,
        limit,
        stages: stageSummary,
        cpv: cpv.trim() || 'Any CPV',
        dateFrom: dateFrom || 'Any date',
        dateTo: dateTo || 'Any date',
      });
    } catch (err) {
      setError(err.message || 'Search failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">FT</div>
          <div>
            <div className="brand-title">Find Tender Counsel</div>
            <div className="brand-subtitle">Advanced tender search for legal teams</div>
          </div>
        </div>
        <div className="topbar-actions">
          <button className="ghost-button" type="button">Saved Searches</button>
          <button className="primary-button" type="button">New Brief</button>
        </div>
      </header>

      <main className="page">
        <section className="hero">
          <div>
            <h1>Explore live and archived procurement notices with confidence.</h1>
            <p>
              Build precise queries, layer CPV codes, and narrow by timeline or stage — all optimized for
              long research sessions.
            </p>
          </div>
          <div className="hero-card">
            <div className="hero-stat">
              <span>Database</span>
              <strong>Local & searchable</strong>
            </div>
            <div className="hero-stat">
              <span>Latency</span>
              <strong>Sub-second</strong>
            </div>
            <div className="hero-stat">
              <span>Coverage</span>
              <strong>UK public tenders</strong>
            </div>
          </div>
        </section>

        <section className="workspace">
          <form className="filters-panel" onSubmit={handleSearch}>
            <div className="panel-header">
              <div>
                <h2>Advanced Search</h2>
                <p>Compose a query and refine it with structured filters.</p>
              </div>
              <button className="ghost-button" type="button" onClick={clearFilters}>
                Clear filters
              </button>
            </div>

            <div className="field-block">
              <label htmlFor="keywords">Keywords</label>
              <input
                id="keywords"
                type="text"
                value={keywords}
                onChange={(event) => setKeywords(event.target.value)}
                placeholder="e.g., digital transformation, housing repairs"
              />
              <div className="inline-options">
                <button
                  type="button"
                  className={mode === 'exact' ? 'chip chip-active' : 'chip'}
                  onClick={() => setMode('exact')}
                >
                  Exact phrase
                </button>
                <button
                  type="button"
                  className={mode === 'near' ? 'chip chip-active' : 'chip'}
                  onClick={() => setMode('near')}
                >
                  Near match
                </button>
              </div>
            </div>

            <div className="field-block">
              <label htmlFor="cpv">CPV codes</label>
              <input
                id="cpv"
                type="text"
                value={cpv}
                onChange={(event) => setCpv(event.target.value)}
                placeholder="e.g., 72000000, 72260000"
              />
              <span className="helper">Separate multiple CPV codes with commas.</span>
            </div>

            <div className="field-block">
              <label>Stages</label>
              <div className="stage-grid">
                {STAGES.map((stage) => (
                  <label key={stage.value} className="stage-pill">
                    <input
                      type="checkbox"
                      checked={stages.includes(stage.value)}
                      onChange={() => handleStageToggle(stage.value)}
                    />
                    <span>{stage.label}</span>
                  </label>
                ))}
              </div>
              <span className="helper">Current selection: {stageSummary}</span>
            </div>

            <div className="field-block">
              <label>Publication window</label>
              <div className="date-grid">
                <div>
                  <span className="input-label">From</span>
                  <input
                    type="date"
                    value={dateFrom}
                    onChange={(event) => setDateFrom(event.target.value)}
                  />
                </div>
                <div>
                  <span className="input-label">To</span>
                  <input
                    type="date"
                    value={dateTo}
                    onChange={(event) => setDateTo(event.target.value)}
                  />
                </div>
              </div>
            </div>

            <div className="field-block">
              <label htmlFor="limit">Results limit</label>
              <div className="inline-options">
                {LIMIT_OPTIONS.map((option) => (
                  <button
                    key={option}
                    type="button"
                    className={limit === option ? 'chip chip-active' : 'chip'}
                    onClick={() => setLimit(option)}
                  >
                    {option}
                  </button>
                ))}
              </div>
            </div>

            <button className="primary-button" type="submit" disabled={loading}>
              {loading ? 'Searching…' : 'Search tenders'}
            </button>

            {error ? <div className="alert">{error}</div> : null}
          </form>

          <section className="results-panel">
            <div className="results-header">
              <div>
                <h2>Results</h2>
                <p>Ranked by relevance and publication date.</p>
              </div>
              <div className="results-meta">
                <span>{results.length} items</span>
                <span className="divider" />
                <span>{lastQuery ? lastQuery.mode : '—'}</span>
              </div>
            </div>

            {lastQuery ? (
              <div className="query-pill">
                <span>{lastQuery.keywords}</span>
                <span>{lastQuery.stages}</span>
                <span>{lastQuery.cpv}</span>
                <span>{lastQuery.dateFrom} → {lastQuery.dateTo}</span>
              </div>
            ) : (
              <div className="query-pill placeholder">Run a search to see results.</div>
            )}

            <div className="results-list">
              {loading ? (
                <div className="loading">Gathering tenders…</div>
              ) : null}
              {!loading && results.length === 0 ? (
                <div className="empty-state">
                  <h3>No results yet</h3>
                  <p>Use the filters to build a precise query. Results will appear here.</p>
                </div>
              ) : null}
              {results.map((item) => (
                <article key={item.ocid} className="result-card">
                  <div className="result-head">
                    <div>
                      <h3>{item.title || 'Untitled tender'}</h3>
                      <p>{item.description || 'No description available.'}</p>
                    </div>
                    <div className="score-pill">Score {item.score?.toFixed(2)}</div>
                  </div>
                  <div className="result-meta">
                    <span>OCID: {item.ocid}</span>
                    <span>Published: {item.published_at ? new Date(item.published_at).toLocaleDateString() : '—'}</span>
                  </div>
                  {item.url ? (
                    <a className="result-link" href={item.url} target="_blank" rel="noreferrer">
                      Open notice
                    </a>
                  ) : null}
                </article>
              ))}
            </div>
          </section>
        </section>
      </main>
    </div>
  );
}

export default App;
