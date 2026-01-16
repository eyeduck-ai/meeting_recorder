FROM python:3.12-slim-bookworm

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Taipei

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Media & Display
    ffmpeg \
    xvfb \
    pulseaudio \
    x11vnc \
    # Utilities
    curl \
    wget \
    dbus \
    procps \
    # Build dependencies
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Set working directory
WORKDIR /app

# Add a build argument to force cache invalidation for the copy step
ARG BUILD_TIMESTAMP=unknown

# Copy all application code
COPY . .

# Copy .env.example to .env if .env doesn't exist
RUN cp -n .env.example .env 2>/dev/null || true

# Install Python dependencies with uv
RUN uv pip install --system -e .

# Install Playwright and Chromium dependencies
# playwight install-deps handles all the library dependencies
RUN playwright install chromium && \
    playwright install-deps chromium

# Create directories for volumes
RUN mkdir -p /app/recordings /app/diagnostics /app/data

# Set up PulseAudio for system mode
RUN mkdir -p /var/run/pulse /etc/pulse && \
    # Add root to pulse-access group if it exists, or create it
    (groupadd -f pulse-access && usermod -a -G pulse-access root) || true && \
    # Allow connections to PulseAudio
    echo "default-server = unix:/var/run/pulse/native" > /etc/pulse/client.conf

# Expose API port and VNC port (for debugging)
EXPOSE 8000 5900

# Start script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
