# NEXO Backend (MVP)

FastAPI backend for NEXO store MVP:
- List products
- Create orders
- Bank transfer payment proof upload (Supabase Storage)
- Manual admin review (approve/reject)

## Tech Stack
- FastAPI + Uvicorn
- PostgreSQL (Supabase)
- Supabase Storage
- SQLAlchemy + psycopg2

## Prerequisites
- Python 3.11+ recommended
- A Supabase project (Postgres + Storage bucket)
- Git

## Project Structure
- `main.py` : FastAPI app
- `requirements.txt` : Python dependencies
- `.env.example` : environment variables template (NO secrets)
- `.gitignore` : excludes `.env`, venv, caches
- `scripts/setup_windows.bat` : one-click setup for Windows (optional)

## Environment Variables
Create a `.env` file next to `main.py` (DO NOT commit it):

```env
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_BUCKET=payment-proofs
DATABASE_URL=
ADMIN_API_KEY=
