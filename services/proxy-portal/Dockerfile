# The proxy portal (README.md). Runs as uid/gid 3089, the owner of the applier's spool requests
# (applier/README.md); its database and secret key live in /data, a volume owned by that uid.
FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt requirements-build.txt /src/
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --require-hashes --no-deps -r /src/requirements.txt \
 && python -m venv /opt/build && /opt/build/bin/pip install --require-hashes --no-deps -r /src/requirements-build.txt
COPY portal/locale /src/locale
RUN /opt/build/bin/pybabel compile --statistics -d /src/locale -D django

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH PROXY_PORTAL_DATA_DIR=/data
RUN groupadd --system --gid 3089 proxy-portal \
 && useradd --system --uid 3089 --gid 3089 --no-create-home --shell /usr/sbin/nologin proxy-portal \
 && install -d -o 3089 -g 3089 -m 700 /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY manage.py /app/
COPY portal /app/portal
COPY --from=build /src/locale /app/portal/locale
USER 3089:3089
EXPOSE 8000
# One worker with threads: the cache of Prometheus results is per process.
CMD ["sh", "-c", "umask 077 && python manage.py migrate --noinput && exec gunicorn portal.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 60 --access-logfile -"]
