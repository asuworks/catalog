#!/bin/sh
set -e

/bin/sh /code/deploy/docker/common.sh
cd /code
python3 manage.py collectstatic --noinput --clear
chmod a+x /etc/cron.daily/*
chmod a+x /etc/cron.monthly/*
# Block until the release's configured Elasticsearch endpoint is
# reachable before the application starts.
ES_HOST="${ELASTICSEARCH_HOST:-elasticsearch}"
ES_PORT="${ELASTICSEARCH_PORT:-9200}"
/code/deploy/docker/wait-for-it.sh -t 0 "${ES_HOST}:${ES_PORT}" -- echo "Elasticsearch is ready (${ES_HOST})."
echo "Starting Gunicorn"
# Gunicorn 26.2.0 over the shared unix socket (replaces the legacy
# uWSGI, which has been removed from the deployment):
#   - workers 4 / threads 2  == uwsgi processes/threads
#   - umask 002              == uwsgi chmod-socket 664
#   - timeout 0              == uwsgi had no harakiri (no request timeout)
#   - no stats socket: gunicorn has no uwsgi-style stats equivalent
# X-Forwarded-Proto (set by Nginx) is mapped to wsgi.url_scheme by
# gunicorn's default secure_scheme_headers for trusted unix-socket peers.
mkdir -p /shared/logs /catalog/socket
exec gunicorn catalog.wsgi:application \
    --bind unix:/catalog/socket/gunicorn.sock \
    --workers 4 \
    --threads 2 \
    --umask 002 \
    --timeout 0 \
    --env DJANGO_SETTINGS_MODULE=catalog.settings.prod \
    --error-logfile /shared/logs/gunicorn-error.log \
    --access-logfile /shared/logs/gunicorn-access.log
