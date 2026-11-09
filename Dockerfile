# The Playwright image ships Chromium plus every system library it needs.
# The image tag and the pip package must be the same version, so both come
# from this one build argument.
ARG PLAYWRIGHT_VERSION=1.63.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99 \
    TRACKER_CONFIG=/config/config.toml \
    TRACKER_DATA_DIR=/data

# Virtual display, a tiny window manager, and noVNC so the browser can be
# viewed from a laptop through `kubectl port-forward`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify fluxbox tini \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

RUN pip install --no-cache-dir --break-system-packages "playwright==${PLAYWRIGHT_VERSION}"

RUN useradd --uid 10001 --create-home --shell /bin/bash tracker \
 && mkdir -p /data /config && chown tracker:tracker /data

WORKDIR /app
COPY tracker ./tracker
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER 10001
EXPOSE 6080
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["run"]
