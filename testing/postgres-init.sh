#!/bin/sh
set -eu
case "$POSTGRES_DB" in
  copilot_review_test) ;;
  *) echo "Refusing bootstrap outside review database" >&2; exit 1 ;;
esac
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$BOT_APP_PASSWORD" --file=/opt/review-app.sql
