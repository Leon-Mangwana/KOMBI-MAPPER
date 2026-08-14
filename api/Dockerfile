# Production image for the API. Build context is ./api.
#
# The pipeline is deliberately not in here: it is a batch job with a much
# heavier dependency set (GeoPandas, scikit-learn, Shapely) and it needs OSRM,
# which needs gigabytes of prepared road network. Keeping it out is what lets
# this image stay small enough for a free tier. See ../pipeline/README.md.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them. psycopg ships
# as a binary wheel, so no libpq or compiler is needed here.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000

# Migrations run at boot rather than in a separate release step, because the
# free tiers this targets have no pre-deploy hook. `alembic upgrade head` is a
# no-op once the schema is current, so restarts are cheap; if it fails the
# container exits rather than serving against a schema it does not match.
#
# Shell form on purpose: $PORT is set by the host (Render, Fly) and must expand.
CMD alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
