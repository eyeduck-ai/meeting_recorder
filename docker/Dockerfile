FROM ubuntu:22.04

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Taipei

# Install system dependencies and Python 3.12
RUN apt-get update && apt-get install -y \
    # Required for adding PPA
    software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    # Python 3.12
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    # FFmpeg with codecs
    ffmpeg \
    # Virtual display
    xvfb \
    # Virtual audio
    pulseaudio \
    # VNC for debugging
    x11vnc \
    # Browser dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    # Utilities
    curl \
    wget \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# Set up Python 3.12 as default
RUN ln -sf /usr/bin/python3.12 /usr/bin/python3 && \
    ln -sf /usr/bin/python3 /usr/bin/python

# Install uv for fast package management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Set working directory
WORKDIR /app

# Add a build argument to force cache invalidation for the copy step
ARG BUILD_TIMESTAMP=unknown

# Copy all application code first
COPY . .

# Copy .env.example to .env if .env doesn't exist
RUN cp -n .env.example .env 2>/dev/null || true

# Install Python dependencies with uv
RUN uv pip install --system -e .

# Install Playwright and Chromium
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
