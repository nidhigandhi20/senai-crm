# SenAI CRM

A self-hosted customer email triage and response system built on FastAPI, PostgreSQL, local LLM inference via Ollama, RAG policy retrieval, and structured agent decision logic.

## Overview

SenAI CRM ingests customer emails, classifies them automatically, applies safety and escalation rules, and exposes a backend API for thread inspection, reply workflows, analytics, and knowledge-based reasoning.

Key capabilities:
- Email ingestion and deduplication
- Heuristic pre-filtering for security, legal, and spam queues
- LLM-driven classification with RAG-enhanced policy context
- Sentiment tracking and churn-risk awareness
- Draft approval and reply workflows
- Knowledge base seeding from markdown policy docs
- Comprehensive audit trail for email and action records

## Repository Structure

- `backend/`
  - `api/main.py` — FastAPI routes and API entrypoints
  - `classifier/` — LLM classification engine, prompts, schema validation
  - `agent/` — agent planning, dry-run, and tools for replies + RAG search
  - `db/` — SQLAlchemy models, database session setup, migrations
  - `heuristics/` — pre-filter logic for routing before the LLM
  - `intelligence/` — web reputation scraping and cache storage
  - `pipeline_runner.py` — batch classification runner for unprocessed emails
  - `sentiment/` — sentiment tracker and deterioration detection
  - `rag/` — retrieval-augmented generation pipeline and KB seeding
- `scripts/`
  - `seed_db.py` — populate DB with sample email dataset and contacts
  - `seed_kb.py` — embed knowledge base documents into PostgreSQL
- `knowledge_base/` — policy docs used for RAG retrieval
- `email-data-advanced.json` — sample email dataset used for seeding
- `requirements.txt` — Python dependencies
- `trade-off.txt` — architecture choices and trade-offs

## Setup

### 1. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 2. PostgreSQL

Create a PostgreSQL database for the project. Example:

```bash
createdb senai_crm
```

If you need custom credentials or host settings, set `DATABASE_URL` in a `.env` file.

### 3. Create `.env`

Create a file named `.env` in the repository root with values such as:

```env
DATABASE_URL=postgresql://postgres:password@localhost:5432/senai_crm
EMAIL_DATA_PATH=email-data-advanced.json
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

### 4. Start Ollama

This project is configured to use a local Ollama model by default.

```bash
ollama serve
```

Then load or run your model, for example:

```bash
ollama run llama3.2
```

The app expects the Ollama API to be available at `http://localhost:11434` by default.

## Environment Variables

The main environment variables used by the system are:

- `DATABASE_URL` — PostgreSQL connection string
- `EMAIL_DATA_PATH` — path to the seed email JSON file
- `OLLAMA_BASE_URL` — Ollama API base URL (default `http://localhost:11434`)
- `OLLAMA_MODEL` — local Ollama model name (default `llama3.2`)

## Seeding the Database

Populate the database with sample emails, contacts, and threads:

```bash
python scripts/seed_db.py
```

This script is idempotent and will skip already-present records.

## Seeding the Knowledge Base

Seed the RAG KB from markdown policy documents:

```bash
python scripts/seed_kb.py
```

To force a refresh of the KB, add `--force`.

To run retrieval validation after seeding:

```bash
python scripts/seed_kb.py --test
```

## Running the Email Classification Pipeline

Once the DB and KB are seeded, classify unprocessed email rows:

```bash
cd backend
python -m pipeline_runner
```

Useful options:

- `--dry-run` — list emails that would be processed without committing
- `--limit 10` — process only the first 10 emails
- `--reprocess` — reset already-classified emails to `Received` and classify again
- `--concurrency 3` — run multiple classifications in parallel

Example:

```bash
cd backend
python -m pipeline_runner --limit 20
```

## Running the API Server

From the repository root:

```bash
uvicorn backend.api.main:app --reload --port 8000
```

The API is then available at `http://127.0.0.1:8000`.

### Important endpoints

- `POST /api/ingest` — ingest email payloads
- `GET /api/status/{job_id}` — check processing status
- `GET /threads/{contact_email}` — full thread + sentiment summary
- `GET /dashboard/stats` — dashboard counts and trends
- `POST /respond/{email_id}` — send reply and update status
- `PATCH /drafts/{id}` — update draft content
- `POST /drafts/{id}/approve` — approve and execute draft reply
- `GET /analytics/sentiment-trend` — sentiment time series
- `GET /analytics/category-breakdown` — category distribution
- `GET /rag/search` — debug RAG retrieval
- `GET /intelligence/reputation` — cached company reputation data
- `GET /contacts/{email}` — contact profile and open threads
- `PATCH /contacts/{email}/status` — update contact status

## Email Simulation

There are two main ways to simulate email processing:

1. **Seed a batch of sample emails**
   - Run `python scripts/seed_db.py`
   - Then classify them with `python -m pipeline_runner`

2. **Ingest a single email through the API**
   - Use `POST /api/ingest` with JSON payload:

```json
{
  "message_id": "msg_999",
  "sender": "user@example.com",
  "subject": "Need help with my invoice",
  "body": "I did not receive an invoice for last month.",
  "thread_id": "thread_invoice_999",
  "timestamp": "2026-05-21T12:00:00Z"
}
```

Then poll the new job:

```bash
curl http://127.0.0.1:8000/api/status/999
```

## Architecture and Design Decisions

### Core design

- **FastAPI** for the backend API and lightweight orchestration
- **PostgreSQL + SQLAlchemy** for persistent email, thread, contact, and audit state
- **Ollama** for self-hosted LLM inference to avoid third-party API costs
- **RAG pipeline** using policy documents in `knowledge_base/` for grounded reasoning
- **Heuristic pre-filtering** to immediately route high-risk emails before LLM inference
- **Audit logs** for every important state change

### Why Ollama and local LLMs?

The project uses **Ollama + llama3.2** so it can run locally without billing dependencies. The trade-off is slightly lower reasoning quality than hosted models like Claude Sonnet, but production swap-out is kept easy via the LLM client abstraction.

### Agent workflow

- The classifier first runs a **pre-filter** for spam/security/legal threats
- If the message must be classified, it performs **RAG retrieval** on policy docs
- The LLM receives a prompt with email content, thread history, and policy context
- The result is validated, stored in DB, and the system updates sentiment/churn scoring

### KB seeding

`seed_kb.py` embeds all markdown policy content into PostgreSQL so the LLM can retrieve relevant context quickly during classification and reply generation.

## Known Limitations

- The system is tuned for **demo/sample email workloads** and not optimized for very high throughput.
- Local Ollama inference can be **slow** and may require smaller concurrency values.
- The KB search is limited to the current knowledge documents and does not include live web policy updates.
- The reply workflow assumes an **automated reply action** rather than a full email delivery mechanism.
- Contact reputation scraping is **cached** and relies on the scraper tooling being invoked separately.
- There is no user-facing authentication on the API, so this repo is not yet production-hardened.

## Troubleshooting

- If the API cannot connect to PostgreSQL, verify `DATABASE_URL` and that the database is running.
- If classification fails, confirm Ollama is reachable at `OLLAMA_BASE_URL` and the specified `OLLAMA_MODEL` is loaded.
- If RAG retrieval returns no chunks, run `python scripts/seed_kb.py --force` to refresh the KB.

## Useful commands

```bash
pip install -r requirements.txt
python scripts/seed_db.py
python scripts/seed_kb.py --test
cd backend && python -m pipeline_runner --limit 10
uvicorn backend.api.main:app --reload --port 8000
```

## Additional notes

- Policy docs live in `knowledge_base/`
- Email seed data is `email-data-advanced.json`
- `trade-off.txt` documents the main decision to use Ollama for self-hosting
- `backend/api/main.py` now contains the full production-style endpoint set for the assignment
