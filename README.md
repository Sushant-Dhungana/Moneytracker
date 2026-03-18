# arthaX Python Backend

Initial implementation for strict architecture:

Frontend -> Backend API -> PostgreSQL

## Implemented endpoints

- `GET /api/v1/health`
- `GET /api/v1/health/db`
- `GET /api/v1/profile`
- `GET /api/v1/transactions/feed`
- `GET /api/v1/accounts/balances`
- `POST /api/v1/ai/personal/chat`
- `POST /api/v1/ai/business/chat`

## Auth

All non-health endpoints require:

`Authorization: Bearer <supabase_access_token>`

The backend verifies JWT using Supabase JWKS.

## Setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
cp .env.example .env
```

Set `.env` values:

- `DATABASE_URL` -> Postgres connection string
- `SUPABASE_URL` -> your Supabase project URL
- `SUPABASE_ANON_KEY` -> recommended for auth token fallback validation
- `DB_CONNECT_TIMEOUT_SEC` -> DB connect timeout in seconds (default `5`)
- `DB_POOL_ACQUIRE_TIMEOUT_SEC` -> pool acquire timeout in seconds (default `8`)
- `PERSONAL_CHAT_API_ENDPOINT` -> personal AI upstream endpoint
- `BUSINESS_CHAT_API_ENDPOINT` -> business AI upstream endpoint
- `EMBEDDING_API_ENDPOINT` -> embedding endpoint for business vector retrieval (optional but recommended)
- `EMBEDDING_MODEL_ID` / `EMBEDDING_DIM` -> must match vector schema settings

Apply latest Supabase migrations before using business AI routes:

- `supabase/migrations/202603090001_ai_chat_history_and_business_vectors.sql`
- `supabase/migrations/202603090003_business_chat_history_tables.sql`
- `supabase/migrations/202603090004_business_vector_objects_cutover.sql`

Business vector indexing runs asynchronously in a backend worker loop at app startup.

## Run

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

## Next migration steps

- Add write endpoints for ledger posting
- Move frontend services to call backend endpoints
- Remove direct Supabase calls from mobile services

#run backend
-create a virtual environment
-install packages mentioned in requirements.txt
-python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
