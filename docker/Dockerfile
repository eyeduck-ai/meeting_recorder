FROM python:3.12-slim-bookworm

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Taipei

# Install system dependencies in a single layer with cleanup
# Using PipeWire instead of PulseAudio for better stability and lower latency
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Media & Display (required)
    ffmpeg \
    xvfb \
    # PipeWire audio stack (required)
    pipewire \
    pipewire-pulse \
    wireplumber \
    # PulseAudio CLI tools (for pactl commands)
    pulseaudio-utils \
    # D-Bus (required for PipeWire)
    dbus \
    dbus-user-session \
    # Minimal utilities
    curl \
    x11vnc \
    # Build dependencies (will be removed after pip install)
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Set working directory
WORKDIR /app

# Copy dependency files first for better caching
COPY pyproject.toml uv.lock README.md ./

# Install pinned Python dependencies from uv.lock
RUN uv export --format requirements.txt --frozen --no-dev --no-editable --no-emit-project \
        --output-file requirements.lock.txt && \
    uv pip install --system --no-cache-dir -r requirements.lock.txt && \
    rm requirements.lock.txt

# Install Playwright Chromium only (skip other browsers)
# Use --with-deps to install browser dependencies
RUN playwright install --with-deps chromium && \
    # Clean up playwright cache (keep only chromium)
    rm -rf /root/.cache/ms-playwright/firefox* \
           /root/.cache/ms-playwright/webkit* \
           /root/.cache/pip \
           /tmp/*

# Remove build dependencies to reduce image size
RUN apt-get purge -y build-essential && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy application code (after dependencies for better caching)
ARG BUILD_TIMESTAMP=unknown
ENV APP_REVISION=${BUILD_TIMESTAMP}
LABEL org.opencontainers.image.revision=${BUILD_TIMESTAMP}
COPY . .

# Install the application itself without re-resolving dependencies
RUN uv pip install --system --no-cache-dir --no-deps -e .

# Runtime owns only persistent data and its home, never application source.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid ${APP_GID} recorder && \
    useradd --uid ${APP_UID} --gid ${APP_GID} --create-home recorder && \
    mkdir -p /app/recordings /app/diagnostics /app/data /app/logs && \
    chown recorder:recorder /app/recordings /app/diagnostics /app/data /app/logs
ENV HOME=/home/recorder
ENV XDG_RUNTIME_DIR=/tmp/meeting-recorder-runtime

# Expose API port
EXPOSE 8000

# Start script
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

USER recorder:recorder
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
