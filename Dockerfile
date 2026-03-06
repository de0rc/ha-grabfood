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

# Install noVNC
RUN git clone --depth 1 https://github.com/novnc/noVNC.git /opt/novnc \
    && git clone --depth 1 https://github.com/novnc/websockify.git /opt/novnc/utils/websockify

# Install Python deps from PyPI directly
RUN pip3 install --no-cache-dir --break-system-packages \
    playwright \
    aiohttp \
    jinja2

# Install Playwright's bundled Chromium (always version-matched)
RUN playwright install chromium \
    && playwright install-deps chromium

WORKDIR /app
COPY start.sh /start.sh
RUN chmod +x /start.sh
COPY app/ /app/

CMD ["/bin/bash", "/start.sh"]