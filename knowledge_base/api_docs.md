# API Documentation

## Overview
This document covers API authentication, rate limits by plan tier, the v1 deprecation timeline, v2 breaking changes, required headers, webhook configuration, and common error codes. Reference this document when handling integration questions, API errors, or migration inquiries.

---

## Authentication

### API Key Authentication
All API requests must include a valid API key in the request header:

```
Authorization: Bearer YOUR_API_KEY
```

- API keys are generated in Settings → Developer → API Keys
- Each workspace can have up to 10 active API keys
- Keys can be scoped: read-only, read-write, or admin
- Keys do not expire but can be revoked at any time

### Workspace Header (v2 Requirement)
**This is the most common cause of 403 errors on v2.**

All v2 API requests require the workspace ID header:

```
X-Workspace-ID: your-workspace-id
```

- Find your workspace ID in Settings → Workspace → General
- This header is REQUIRED on all v2 endpoints
- Omitting this header returns: `403 Forbidden — Missing X-Workspace-ID header`
- This requirement did NOT exist in v1 — this is a breaking change

---

## Rate Limits by Plan

| Plan | Requests/Minute | Requests/Day | Burst Limit |
|------|----------------|--------------|-------------|
| Starter | 100 req/min | 10,000/day | 200 req/min for 30s |
| Standard | 1,000 req/min | 100,000/day | 2,000 req/min for 30s |
| Professional | 5,000 req/min | 500,000/day | 10,000 req/min for 30s |
| Enterprise | Custom (default 10,000 req/min) | Custom | Negotiable |

### Rate Limit Headers
Every API response includes:
```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 847
X-RateLimit-Reset: 1698765432
```

### When You Hit a Rate Limit
- Response: `429 Too Many Requests`
- Body: `{"error": "rate_limit_exceeded", "retry_after": 23}`
- Implement exponential backoff: wait 1s, then 2s, then 4s, then 8s
- Do not hammer the API after a 429 — this can trigger temporary IP blocking

### Requesting Rate Limit Increases
- Standard → Professional upgrade handles most cases
- Enterprise customers: contact account manager for custom limits
- Temporary increases (e.g. for a data migration): email api-support@company.com

---

## API v1 Deprecation

### Timeline
- **v1 Deprecation Announced:** September 1, 2023
- **v1 Read-Only Mode:** December 1, 2023 (write endpoints disabled)
- **v1 Sunset Date: December 31, 2023** — all v1 endpoints return 410 Gone
- **v2 GA Release:** September 1, 2023

### What This Means
After December 31, 2023, any application still using v1 endpoints will stop working entirely. All integrations must migrate to v2 before this date.

### Migration Support
- Migration guide: docs.company.com/migrate-v1-to-v2
- Migration support email: api-support@company.com
- We offer free migration review calls for Professional and Enterprise customers

---

## v2 Breaking Changes

These changes from v1 to v2 will break existing integrations if not addressed:

### 1. Required X-Workspace-ID Header
- **v1:** Not required
- **v2:** Required on all requests
- **Fix:** Add `X-Workspace-ID: {your_workspace_id}` to all requests

### 2. Authentication Format Change
- **v1:** `Authorization: Token YOUR_API_KEY`
- **v2:** `Authorization: Bearer YOUR_API_KEY`
- **Fix:** Update the Authorization header prefix from "Token" to "Bearer"

### 3. Pagination Format
- **v1:** `?page=1&per_page=20`
- **v2:** `?cursor=eyJpZCI6MTAwfQ&limit=20`
- **Fix:** Implement cursor-based pagination instead of page-based

### 4. Webhook Payload Structure
- **v1:** Flat JSON object
- **v2:** Nested with `event`, `data`, and `metadata` keys
- **Fix:** Update webhook handlers to access `event.data.{field}` instead of `{field}`

### 5. Timestamp Format
- **v1:** Unix timestamp (integer)
- **v2:** ISO 8601 string (`2023-10-01T08:50:00Z`)
- **Fix:** Update timestamp parsing in your application

### 6. Error Response Format
- **v1:** `{"message": "error description"}`
- **v2:** `{"error": {"code": "ERROR_CODE", "message": "...", "details": {...}}}`
- **Fix:** Update error handling to read `error.message` instead of `message`

---

## Common Endpoints

### Base URL
```
https://api.company.com/v2/
```

### Core Endpoints

#### Contacts
```
GET    /v2/contacts              — List all contacts
POST   /v2/contacts              — Create a contact
GET    /v2/contacts/{id}         — Get a contact
PATCH  /v2/contacts/{id}         — Update a contact
DELETE /v2/contacts/{id}         — Delete a contact
```

#### Emails / Threads
```
GET    /v2/threads               — List all threads
GET    /v2/threads/{id}          — Get thread with all emails
POST   /v2/emails                — Ingest a new email
GET    /v2/emails/{id}           — Get a specific email
PATCH  /v2/emails/{id}/status    — Update email status
```

#### Webhooks
```
POST   /v2/webhooks              — Register a webhook endpoint
GET    /v2/webhooks              — List registered webhooks
DELETE /v2/webhooks/{id}         — Remove a webhook
```

---

## Webhook Configuration

### Supported Events
- `email.received` — New email ingested
- `email.classified` — AI classification complete
- `thread.escalated` — Thread escalated to human
- `contact.status_changed` — Contact VIP/Blocked status updated
- `agent.action_taken` — Agent completed a reasoning cycle

### Webhook Security
All webhook payloads are signed with HMAC-SHA256:
```
X-Webhook-Signature: sha256=abc123...
```

Verify the signature using your webhook secret before processing:
```python
import hmac, hashlib
expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
assert f"sha256={expected}" == request.headers["X-Webhook-Signature"]
```

---

## Common Error Codes

| Code | HTTP Status | Meaning | Fix |
|------|-------------|---------|-----|
| `missing_workspace_id` | 403 | X-Workspace-ID header missing | Add the header |
| `invalid_api_key` | 401 | API key invalid or revoked | Generate a new key |
| `rate_limit_exceeded` | 429 | Too many requests | Back off and retry |
| `resource_not_found` | 404 | Entity doesn't exist | Check the ID |
| `validation_error` | 422 | Request body invalid | Check required fields |
| `plan_limit_exceeded` | 402 | Feature not available on current plan | Upgrade plan |
| `endpoint_deprecated` | 410 | v1 endpoint no longer available | Migrate to v2 |

---

## SDKs and Libraries

Official SDKs available:
- **Python:** `pip install company-sdk`
- **Node.js:** `npm install @company/sdk`
- **Ruby:** `gem install company-client`

Community SDKs (not officially supported):
- PHP, Go, Java — available on our GitHub

---

## Getting Help

- API documentation: docs.company.com
- API status: status.company.com
- Support: api-support@company.com
- Enterprise API support: dedicated Slack channel with your account manager