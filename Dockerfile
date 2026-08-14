FROM python:3.12-slim

# Ghostscript normalises uploaded PDFs before they reach a printer. It is a
# runtime dependency, not a build one.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY migrations ./migrations
COPY alembic.ini ./

EXPOSE 8000

# --factory because app/main.py deliberately has no module-level app instance;
# building one at import time would require full config just to import.
#
# Workers > 1 is safe here: the device WebSocket registry lives in Redis, not in
# a per-process dict. That is the constraint the old backend could never lift.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers"]
