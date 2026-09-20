#!/bin/sh

set -eu

# Cron starts jobs with a minimal environment, so preserve the container config.
mkdir -p /shared/logs
umask 077
export -p > /run/catalog-scheduler.env
umask 022
touch /shared/logs/cron.log
chmod 0644 /shared/logs/cron.log
exec /usr/sbin/cron -f
