# Auth Architecture

## Overview

LineUp uses **Clerk** as its identity provider. Clerk issues RS256 JWTs to authenticated frontend users. The backend verifies these tokens by fetching Clerk's public keys (JWKS) and never handles passwords.

```
Frontend (Clerk SDK)
    │
    │  RS256 JWT (Bearer token)
    ▼
Backend (FastAPI)
    │
    │  GET /.well-known/jwks.json  (cached 1 hour)
    ▼
Clerk JWKS endpoint
    │
    │  JWKS (public keys)
    ▼
Backend verifies JWT signature → extracts sub (clerk_id)
    │
    │  SELECT * FROM users WHERE clerk_id = :clerk_id
    ▼
users table → User row → route handler
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLERK_JWKS_URL` | Yes | Clerk public key endpoint, e.g. `https://clerk.yourdomain.com/.well-known/jwks.json` |
| `CLERK_WEBHOOK_SECRET` | Yes | Svix signing secret from Clerk Dashboard (starts with `whsec_`) |
| `CLERK_AUDIENCE` | No | JWT audience claim, e.g. `https://api.lineupmarkets.com`. Leave empty to skip audience validation. |

**Removed variables** (no longer used): `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_EXPIRATION_MINUTES`

## JWKS Caching

`app/core/auth._fetch_jwks()` uses a `cachetools.TTLCache` with a 1-hour TTL and size 1. The cache is populated on the first request and automatically invalidated after one hour, triggering a fresh fetch from Clerk. This keeps network round-trips to a minimum while staying current with Clerk key rotations.

## JWT Verification Flow (`verify_clerk_jwt`)

1. Fetch JWKS (from cache or Clerk)
2. Call `jose.jwt.decode(token, jwks, algorithms=["RS256"], audience=...)`
3. python-jose selects the matching public key from JWKS using the JWT `kid` header
4. Signature, expiry, and audience (if set) are validated
5. `sub` claim is returned — this is the Clerk user ID (`user_xxxx`)
6. Any failure raises `jose.JWTError` → `get_current_user` returns HTTP 401

## Webhook Event Sync

Clerk notifies the backend of user lifecycle events via Svix-signed webhooks.

**Endpoint:** `POST /api/webhooks/clerk`

**Events handled:**

| Event | Action |
|-------|--------|
| `user.created` | Upsert user row, create 1000-point portfolio |
| `user.updated` | Upsert user row (email/username sync), skip portfolio |
| `user.deleted` | Delete user row (cascades to portfolios/positions/trades) |

## Svix Signature Verification

Every webhook request is verified using the `svix` Python library:

```python
wh = Webhook(settings.clerk_webhook_secret)
event = wh.verify(payload_bytes, headers_dict)
```

Svix checks the `svix-id`, `svix-timestamp`, and `svix-signature` headers. Any mismatch raises `WebhookVerificationError` → the endpoint returns HTTP 400.

The raw request body must be read before FastAPI parses it (`await request.body()`), because signature verification is over the raw bytes.

## Rotating the Webhook Secret

1. Generate a new signing secret in the Clerk Dashboard (Webhooks → your endpoint → Signing Secret → Rotate)
2. Update `CLERK_WEBHOOK_SECRET` in your environment / secret manager
3. Redeploy the backend — no code changes required

## User Lookup

`get_current_user` in `app/dependencies.py`:
1. Extracts the Bearer token from the `Authorization` header
2. Calls `await verify_clerk_jwt(token)` → `clerk_id`
3. `SELECT * FROM users WHERE clerk_id = :clerk_id`
4. If no row found → HTTP 401 (user exists in Clerk but webhook hasn't run yet, or was never synced)

## Testing

Tests in `tests/test_auth.py` use a locally generated RSA key pair (via `cryptography` library). `_fetch_jwks` is patched in every test via the `patch_clerk_jwks` autouse fixture in `conftest.py`, so no real Clerk account or network access is needed.

```bash
pytest tests/test_auth.py -v
```
