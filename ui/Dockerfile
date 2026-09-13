# Standalone review-UI container. Serves ui/index.html as a static page,
# separately from the API server in the repo-root Dockerfile - see that
# Dockerfile's docstring-equivalent comment and README.md's "Splitting the
# review UI into its own container" section for why you'd want this.
#
# The API this page talks to is configured entirely client-side (the "Base
# URL" field, or AGENTHIVE_API_URL below to pre-fill it) - this image has
# no knowledge of AGENTHIVE_URL/TOKEN/TEAM_ID and needs none.
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html.template
COPY docker-entrypoint.d/40-render-config.sh /docker-entrypoint.d/40-render-config.sh

USER root
RUN chmod +x /docker-entrypoint.d/40-render-config.sh \
    && chown -R nginx:nginx /usr/share/nginx/html
USER nginx

# AGENTHIVE_API_URL (optional): pre-fills the "Base URL" field so users
# don't have to type it in every time. Purely a convenience - the field
# stays editable, so this doesn't lock the page to one API.
ENV AGENTHIVE_API_URL=""

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
