# ado-exporter — Prometheus exporter for Azure DevOps work items.
#
# Stdlib only, so there is nothing to pip install and no third-party code in
# the image. Runs unprivileged; the only thing it ever needs is a read-only
# bind mount of the ADO PAT at /run/secrets/ado-pat.
FROM python:3.13-slim

# Non-root. UID 9823 matches the exporter's port, the convention used by
# semaphore-exporter on this box.
#
# Getting this wrong fails in a way worth naming: the container starts fine and
# serves /metrics, but every scrape returns 502 "Permission denied:
# /run/secrets/ado-pat", because the failure is at read time, not start time.
RUN useradd --system --uid 9823 --no-create-home --shell /usr/sbin/nologin exporter

COPY exporter.py /app/exporter.py

USER 9823
EXPOSE 9823

# No HEALTHCHECK on purpose: it would poll the ADO API on a timer for no
# benefit, and Prometheus scraping /metrics is already the liveness signal
# (ado_exporter_last_run_timestamp).
ENTRYPOINT ["python3", "/app/exporter.py"]
