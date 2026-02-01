# Repository Guidelines

## Project Structure & Module Organization
- `backend/`: FastAPI service (`main.py`), data clone utilities (`clone.py`), Python deps in `requirements.txt`.
- `frontend/`: React UI (Create React App). Source in `frontend/src/`, static assets in `frontend/public/`.
- `data/`: Local data dumps and clone artifacts (large files, keep out of code changes when possible).
- `logs/`: Runtime logs and ad-hoc output.

## Build, Test, and Development Commands
Backend (from repo root):
- `python -m venv .venv && source .venv/bin/activate`: optional virtualenv setup.
- `pip install -r backend/requirements.txt`: install Python dependencies.
- `uvicorn backend.main:app --reload --port 8000`: run API with hot reload.

Frontend (from `frontend/`):
- `npm install`: install Node dependencies.
- `npm start`: start dev server (proxy to `http://localhost:8000`).
- `npm run build`: production build to `frontend/build/`.
- `npm test`: run Jest in watch mode via react-scripts.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints where already used, favor small helper functions (see `backend/main.py`).
- JavaScript/React: follow Create React App defaults; linting via `eslintConfig` in `frontend/package.json`.
- Naming: snake_case for Python, camelCase for JS, React components in `PascalCase`.
- No explicit formatter config is present; keep changes consistent with existing files.

## Testing Guidelines
- Frontend tests run with `npm test` (Jest/React Scripts). Place tests under `frontend/src/` using `*.test.js`.
- Backend currently has no test framework configured; add tests only if you introduce behavior that needs coverage.

## Commit & Pull Request Guidelines
- Commit history uses short, sentence-case summaries (e.g., “Add FastAPI backend…”). Keep messages concise and descriptive.
- PRs should include: what changed, why, how to run it, and any risk notes.
- Include screenshots for UI changes and link related issues/tickets when applicable.

## Configuration & Data Notes
- Runtime config via environment variables: `DATABASE_URL`, `FIND_TENDER_BASE_URL`, `FIND_TENDER_VERSION`.
- Large JSON dumps live in `data/`; avoid editing or reformatting these in feature PRs.
