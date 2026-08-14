# printvendo-backend

Rebuilt central API for PrintVendo. **Not yet deployed** — `cloud-backend/`
serves production and is untouched by this work.

- Design: `../docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md`
- Build plan: `../docs/superpowers/plans/2026-08-14-backend-foundation.md`
- Working notes and conventions: `CLAUDE.md`

## Quick start

Requires a local Postgres with role `printvendo` and databases `printvendo` and
`printvendo_test`.

```bash
cp .env.example .env      # then fill JWT_SECRET_KEY and SECRETS_ENCRYPTION_KEY
py -3.12 -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m uvicorn app.main:create_app --factory --reload --port 8000
```

Generate the two required secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                    # JWT_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # SECRETS_ENCRYPTION_KEY
```

Health check: <http://localhost:8000/health>
