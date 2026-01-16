#!/bin/bash

echo "=== AutoMeetRecorder Container Starting ==="

# Start D-Bus (required for PulseAudio)
echo "Starting D-Bus..."
if [ ! -d /run/dbus ]; then
    mkdir -p /run/dbus
fi
rm -f /run/dbus/pid 2>/dev/null || true
dbus-daemon --system --fork 2>/dev/null || echo "D-Bus already running or not available"

# Clean up any stale PulseAudio files
echo "Cleaning up stale PulseAudio files..."
rm -rf /var/run/pulse/* /tmp/pulse-* 2>/dev/null || true
mkdir -p /var/run/pulse

# Create PulseAudio system config
mkdir -p /etc/pulse
cat > /etc/pulse/system.pa << 'EOF'
# PulseAudio system mode config for Docker
load-module module-native-protocol-unix auth-anonymous=1
load-module module-null-sink sink_name=virtual_speaker sink_properties=device.description="Virtual_Speaker"
set-default-sink virtual_speaker
EOF

# Create client config to connect to system daemon
cat > /etc/pulse/client.conf << 'EOF'
autospawn = no
default-server = unix:/var/run/pulse/native
EOF

# Kill any existing PulseAudio
pulseaudio --kill 2>/dev/null || true
sleep 1

# Start PulseAudio in SYSTEM mode (required for running as root in Docker)
echo "Starting PulseAudio in system mode..."
pulseaudio --system --daemonize --disallow-exit \
    --disallow-module-loading=0 \
    --log-level=warning \
    --high-priority=no \
    --realtime=no \
    --exit-idle-time=-1 2>&1

# Wait for PulseAudio to be ready (with retry)
PULSE_READY=0
for i in $(seq 1 10); do
    sleep 0.5
    if pactl info >/dev/null 2>&1; then
        PULSE_READY=1
        break
    fi
    echo "Waiting for PulseAudio... ($i/10)"
done

if [ "$PULSE_READY" = "1" ]; then
    echo "PulseAudio started successfully"

    # Verify virtual sink exists, create if not
    if ! pactl list sinks short 2>/dev/null | grep -q virtual_speaker; then
        echo "Creating virtual_speaker sink..."
        pactl load-module module-null-sink sink_name=virtual_speaker \
            sink_properties=device.description="Virtual_Speaker" 2>/dev/null || true
    fi

    # Set default sink
    pactl set-default-sink virtual_speaker 2>/dev/null || true

    # List audio devices for debugging
    echo "=== Audio Configuration ==="
    echo "Sinks:"
    pactl list sinks short 2>/dev/null || true
    echo "Sources:"
    pactl list sources short 2>/dev/null || true
    echo "==========================="
else
    echo "WARNING: PulseAudio failed to start. Audio recording will use silent fallback."
fi

# Start VNC server if DEBUG_VNC is set
if [ "${DEBUG_VNC:-0}" = "1" ]; then
    echo "Starting VNC server on port 5900 for debugging..."

    # 从环境变量读取分辨率，默认 1920x1080
    VNC_WIDTH="${RESOLUTION_W:-1920}"
    VNC_HEIGHT="${RESOLUTION_H:-1080}"
    VNC_DEPTH="24"
    VNC_DISPLAY=":99"

    # 清理旧的 lock 文件
    if [ -f "/tmp/.X99-lock" ]; then
        echo "Removing stale X lock file..."
        rm -f /tmp/.X99-lock
    fi

    # 检查 Xvfb 是否已运行
    if ! pgrep -x Xvfb > /dev/null; then
        echo "Starting Xvfb with resolution ${VNC_WIDTH}x${VNC_HEIGHT}..."
        Xvfb ${VNC_DISPLAY} -screen 0 ${VNC_WIDTH}x${VNC_HEIGHT}x${VNC_DEPTH} \
            -ac +extension GLX +extension RANDR +extension RENDER &
        sleep 2
    else
        echo "Xvfb already running, reusing existing instance"
    fi

    export DISPLAY=${VNC_DISPLAY}
    x11vnc -display ${VNC_DISPLAY} -forever -nopw -shared -rfbport 5900 -bg -o /tmp/x11vnc.log 2>/dev/null
    echo "VNC server started - connect to localhost:5900 (${VNC_WIDTH}x${VNC_HEIGHT})"
fi

echo "=== Starting Application ==="
# Execute the main command
exec "$@"
