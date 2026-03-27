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
    # Build dependencies (will be removed after pip install)
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

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
COPY . .

# Install the application itself without re-resolving dependencies
RUN uv pip install --system --no-cache-dir --no-deps -e .

# Copy .env.example to .env if .env doesn't exist
RUN cp -n .env.example .env 2>/dev/null || true

# Create directories for volumes
RUN mkdir -p /app/recordings /app/diagnostics /app/data /app/logs

# Set up PipeWire runtime directory
ENV XDG_RUNTIME_DIR=/run/user/0
RUN mkdir -p /run/user/0 && chmod 700 /run/user/0 && \
    mkdir -p /etc/pipewire/pipewire.conf.d && \
    mkdir -p /etc/pipewire/pipewire-pulse.conf.d

# Expose API port
EXPOSE 8000

# Start script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
