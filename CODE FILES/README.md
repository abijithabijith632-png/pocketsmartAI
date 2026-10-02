# PocketSmart AI

PocketSmart AI is a FastAPI budget-planning application with authenticated Home, Party, and Jewelry planners. Gemini generates recommendations when available; the application has a local fallback for temporary Gemini service failures.

## Requirements

- Python and pip
- Node.js and npm (only needed for the Playwright E2E suite)
- A Gemini API key for AI generated recommendations

## Configure

1. Copy `.env.example` to `.env`.
2. Set `GEMINI_API_KEY` to your own key and replace `SECRET_KEY` with a long random value.
3. Keep `.env` private; it is ignored by Git.

The backend also accepts `GOOGLE_API_KEY` as an alternative Gemini key variable.

## Run the application

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. The Gemini-independent health endpoint is `/health`.

## Run the browser checks

```powershell
npm install
npx playwright install chromium
npm run test:e2e
```

The E2E suite is in `tests/e2e.test.js` and currently contains three end-to-end workflows.

## Notes

User accounts, sessions, and recommendation history currently use in-memory storage and are cleared when the application process restarts. New uploaded outfit images under `static/uploads/` are ignored by Git. Files already tracked in that folder remain tracked until explicitly removed from the repository index.
