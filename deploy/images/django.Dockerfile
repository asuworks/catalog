FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS base

ARG RUN_SCRIPT=./deploy/docker/dev.sh
ARG CATALOG_REVISION=unknown
ARG CITATION_REVISION=unknown
ARG CATALOG_SOURCE=https://github.com/comses/catalog
ARG CATALOG_VERSION=unknown

LABEL org.opencontainers.image.source="${CATALOG_SOURCE}" \
      org.opencontainers.image.revision="${CATALOG_REVISION}" \
      org.opencontainers.image.version="${CATALOG_VERSION}" \
      org.comses.catalog.citation-revision="${CITATION_REVISION}"

# OS-level operational tooling preserved from the legacy Focal image:
# mail relay plus git/curl for ops. PostgreSQL maintenance uses the
# version-matched tools in the database container. The legacy
# build toolchain (libpq-dev, libxml2-dev, python3-dev, python3-pip,
# python3-setuptools) is no longer needed: the locked uv environment
# installs prebuilt wheels only.
RUN apt-get update \
    && apt-get install --no-install-recommends -q -y \
        cron \
        curl \
        git \
        ssmtp \
    && rm -rf /var/lib/apt/lists/*

# Reproducible dependency management, pinned to the uv version that
# produces and validates the root uv.lock.
RUN pip install --no-cache-dir "uv==0.10.10"

WORKDIR /code

# The root project resolves "citation" as an editable path dependency, so
# the citation checkout must be in place before the locked sync can build
# and install it.
COPY citation /code/citation
COPY pyproject.toml uv.lock /code/

# Install the locked production dependencies (no dev dependency group).
# The venv lives outside /code so the dev `.: /code` bind mount can never
# shadow or clobber it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --locked --no-dev
ENV PATH="/opt/venv/bin:$PATH"

COPY deploy/mail/ssmtp.conf /etc/ssmtp/ssmtp.conf
# Install maintenance jobs for the dedicated scheduler service.
COPY deploy/cron/daily_catalog_tasks /etc/cron.daily/
COPY deploy/cron/monthly_catalog_tasks /etc/cron.monthly/
# Gunicorn socket dir (staging/prod also mount a named volume at this path)
RUN chmod +x /etc/cron.daily/daily_catalog_tasks \
    && chmod +x /etc/cron.monthly/monthly_catalog_tasks \
    && mkdir -p /catalog/socket /etc/service/django

COPY . /code

COPY ${RUN_SCRIPT} /etc/service/django/run
RUN chmod a+x /etc/service/django/run

# The legacy image started this script through runit (/sbin/my_init);
# running the same script directly preserves the foreground service
# behavior (dev.sh/prod.sh both exec their server process).
CMD ["/etc/service/django/run"]
