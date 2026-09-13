#!/bin/sh
# Runs on container start (nginx's own docker-entrypoint.sh executes every
# script in /docker-entrypoint.d/ before starting nginx). Renders
# index.html.template -> index.html, substituting AGENTHIVE_API_URL into
# the placeholder set in ui/index.html's inline <script>. AGENTHIVE_API_URL
# is operator-supplied (an env var on this container, not user input), and
# '#' is used as the sed delimiter since a URL never contains it.
set -eu

api_url="${AGENTHIVE_API_URL:-}"
sed "s#AGENTHIVE_DEFAULT_API_URL_PLACEHOLDER#${api_url}#g" \
    /usr/share/nginx/html/index.html.template > /usr/share/nginx/html/index.html
