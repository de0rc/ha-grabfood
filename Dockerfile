ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    # Virtual display
    xvfb \
    # VNC server
    x11vnc \
    # noVNC dependencies
    git \
    # Misc
    bash \
    curl \
    ca-certificates \
    procps \
    xauth \
    && rm -rf /var/lib/apt/lists/*

# Install noVNC (pinned to stable release)
RUN git clone --depth 1 --branch v1.5.0 https://github.com/novnc/noVNC.git /opt/novnc \
    && git clone --depth 1 --branch v0.11.0 https://github.com/novnc/websockify.git /opt/novnc/utils/websockify

# Install Python deps from PyPI directly (pinned for reproducibility)
RUN pip3 install --no-cache-dir --break-system-packages \
    playwright==1.58.0 \
    aiohttp==3.13.3 \
    jinja2==3.1.6

# Install Playwright's bundled Chromium (always version-matched)
RUN playwright install chromium \
    && playwright install-deps chromium

WORKDIR /app
COPY start.sh /etc/services.d/grabfood/run
RUN chmod +x /etc/services.d/grabfood/run
# config.yaml changes on every version bump — copying it first ensures Docker
# invalidates the COPY app/ layer whenever the version changes.
COPY config.yaml /app/config.yaml
COPY app/ /app/