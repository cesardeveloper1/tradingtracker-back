# 🚀 Yape Tracker Backend — Railway Deployment Guide

## Phase 4: Backend with PostgreSQL + Public API

This backend provides:
- ✅ Flask API with SQLAlchemy ORM
- ✅ PostgreSQL support (production) with SQLite fallback (development)
- ✅ Automatic data capture from mobile notifications
- ✅ Public API with rate-limited API keys
- ✅ Swagger/OpenAPI documentation at `/api/docs`
- ✅ Analytics endpoints (monthly, categories, trends, contacts)

---

## Prerequisites

1. **Railway Account**: https://railway.app
2. **GitHub Repository**: Your Yape Tracker repo linked to Railway
3. **Command Line Tools**: `curl`, `git`

---

## Step 1: Railway Setup with PostgreSQL

### Option A: Via Railway Dashboard (Recommended)

1. Go to [railway.app](https://railway.app) and login
2. Create a **New Project**
3. Add a **PostgreSQL** plugin:
   - Click "+ Add Service"
   - Search "PostgreSQL"
   - Click "Add"
4. Add a **GitHub Deployment** for your repo:
   - Click "+ Add Service"
   - Select "GitHub Repository"
   - Connect your Yape Tracker repo
   - Set the **Root Directory** to `backend/`
5. Configure environment variables:
   - Railway will auto-populate `DATABASE_URL` from PostgreSQL
   - Add `MASTER_API_KEY` = `your-secure-random-key-here` (generate with `openssl rand -hex 32`)
   - Add `FLASK_ENV` = `production`

### Option B: Via Railway CLI

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login to Railway
railway login

# Create project in the backend directory
cd backend
railway init

# Add PostgreSQL plugin
railway add

# Set environment variables
railway variables set MASTER_API_KEY="your-secure-key"
railway variables set FLASK_ENV="production"

# DATABASE_URL is automatically set by Railway PostgreSQL plugin
```

---

## Step 2: Verify Deployment

After Railway deploys automatically:

```bash
# Check if backend is running
curl https://your-railway-project-url.up.railway.app/api/ping
# Response: {"status": "ok", "message": "Yape Tracker API funcionando"}

# Access Swagger docs
# Visit: https://your-railway-project-url.up.railway.app/api/docs
```

---

## Step 3: Create Your First API Key

The backend needs API keys for external integrations. 

### Option A: Direct Database Insert

```bash
# SSH into Railway PostgreSQL or use pgAdmin
# Insert master admin key (ONE TIME ONLY):

psql -c "INSERT INTO api_key (key_hash, nombre, permisos, rate_limit, activa) 
         VALUES (
           'sha256_hash_of_your_master_key',
           'Master Admin',
           'read,write,admin',
           1000,
           true
         );"
```

### Option B: Use Backend Endpoint

**First, create a quick script to generate a key:**

```python
# generate_api_key.py
import hashlib
import secrets

def generate_api_key():
    raw_key = secrets.token_urlsafe(32)  # Strong random key
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, key_hash

if __name__ == "__main__":
    raw, hashed = generate_api_key()
    print(f"Raw Key (save this): {raw}")
    print(f"Hash (store in DB):  {hashed}")
```

**Then insert into database:**

```bash
python generate_api_key.py
# Copy the output and insert via pgAdmin or Railway's database UI
```

---

## Step 4: Test Public API Endpoints

### Test with Master API Key

```bash
# Replace YOUR_API_KEY with the master key you created
MASTER_KEY="your-generated-api-key-here"
BASE_URL="https://your-railway-url.up.railway.app"

# Test API key validation
curl -X GET "${BASE_URL}/api/auth/validate" \
  -H "X-API-Key: ${MASTER_KEY}"

# Response should be:
# {
#   "valida": true,
#   "detalle": {
#     "id": 1,
#     "nombre": "Master Admin",
#     "permisos": "read,write,admin",
#     "rate_limit": 1000,
#     "activa": true,
#     "created_at": "2026-06-15T..."
#   }
# }

# Test verify-payment (for commerce integration)
curl -X GET "${BASE_URL}/api/verify-payment" \
  -H "X-API-Key: ${MASTER_KEY}" \
  -G \
  --data-urlencode "monto=150.00" \
  --data-urlencode "nombre=Juan Perez" \
  --data-urlencode "fecha=2026-06-15"

# Test analytics (get monthly summary)
curl -X GET "${BASE_URL}/api/analytics/monthly" \
  -H "X-API-Key: ${MASTER_KEY}"
```

---

## API Key Permissions

When creating new API keys, assign permissions as CSV:

| Permission | Allowed Operations |
|------------|-------------------|
| `read`     | GET endpoints (view data, verify payments) |
| `write`    | POST/PUT/DELETE (create/update data) |
| `admin`    | Everything + create/revoke other API keys |

---

## Endpoints Overview

### Public API (Require `X-API-Key` header)

```
GET    /api/auth/validate              — Check if API key is valid
GET    /api/verify-payment             — Verify payment by amount/name/date
GET    /api/analytics/monthly          — Monthly income/expense summary
GET    /api/analytics/categories       — Spending by category
GET    /api/analytics/trends           — Income/expense trends over time
GET    /api/analytics/contacts         — Top contacts by frequency
GET    /api/health                     — Health check + DB stats
```

### Mobile App Endpoints (No auth required)

```
GET    /api/ping                       — Status check
GET    /api/dashboard                  — Dashboard stats
GET    /api/ingresos                   — List all income
POST   /api/ingresos                   — Create income
GET    /api/egresos                    — List all expenses
POST   /api/egresos                    — Create expenses
GET    /api/notificaciones             — List notifications
POST   /api/notificaciones             — Create notification (from Android)
GET    /api/conexiones                 — Connection logs
```

---

## Rate Limiting

Each API key has a `rate_limit` (default: 100 requests/minute).

```
Rate limit exceeded response:
{
  "error": true,
  "mensaje": "Límite de velocidad excedido (100 req/min)"
}
HTTP 429 Too Many Requests
```

---

## Database Migrations (If Needed)

If you modify models in `app.py`, the database schema updates automatically on the next deployment thanks to `db.create_all()`.

**For production, safer approach:**

```python
# Add this to app.py after deploying:
from flask_migrate import Migrate, MigrateCommand

migrate = Migrate(app, db)
```

Then run: `flask db upgrade`

---

## Troubleshooting

### "No PostgreSQL connection"
- Check `DATABASE_URL` in Railway variables
- Ensure PostgreSQL service is running in Railway
- Test locally: `psql $DATABASE_URL`

### "API key invalid"
- Verify the key hash in database matches the one you're sending
- Check `X-API-Key` header spelling (case-sensitive)
- Ensure the key has `activa = true`

### "Rate limit exceeded"
- Check the `rate_limit` column for this API key
- Create a new key with higher `rate_limit`
- Contact admin to increase limit

### "502 Bad Gateway"
- Check Railway logs: Settings → Logs
- Likely issue: missing dependency in requirements.txt
- Fix: Add to requirements, commit, and Railway redeploys automatically

---

## Security Best Practices

1. **Never commit MASTER_API_KEY** to Git (use Railway variables)
2. **Generate strong keys**: `openssl rand -hex 32`
3. **Rotate keys regularly**: Create new ones, revoke old ones
4. **Use different keys per service**: One for commerce, one for dashboards, etc.
5. **Monitor usage**: Check connection logs in `/api/conexiones`

---

## Development Locally

```bash
cd backend

# Install dependencies
pip install -r requirements.txt

# Create .env file
cat > .env << EOF
DATABASE_URL=  # Leave empty for SQLite
MASTER_API_KEY=dev-key
FLASK_ENV=development
EOF

# Run locally
python -m flask run

# Test
curl http://localhost:5000/api/ping
```

---

## Swagger Documentation

View interactive API docs at:
```
https://your-railway-url.up.railway.app/api/docs
```

All endpoints are documented with request/response examples.

---

## Support

For issues:
1. Check Railway logs
2. Review `/api/docs` for endpoint specs
3. Test manually with curl
4. Check database directly with pgAdmin (Railway provides web UI)
