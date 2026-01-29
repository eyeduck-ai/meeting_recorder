#!/bin/bash

echo "=== AutoMeetRecorder Container Starting ==="

# Set up XDG_RUNTIME_DIR for PipeWire
export XDG_RUNTIME_DIR=/run/user/0
mkdir -p $XDG_RUNTIME_DIR
chmod 700 $XDG_RUNTIME_DIR

# Start D-Bus (required for PipeWire/WirePlumber)
echo "Starting D-Bus..."
if [ ! -d /run/dbus ]; then
    mkdir -p /run/dbus
fi
rm -f /run/dbus/pid 2>/dev/null || true
dbus-daemon --system --fork 2>/dev/null || echo "D-Bus system daemon already running"

# Start a D-Bus session bus (needed for PipeWire)
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    # Create session bus socket directory
    mkdir -p $XDG_RUNTIME_DIR/bus
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus/session"
    # Start session bus daemon manually
    dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork --print-pid > /tmp/dbus-session.pid 2>/dev/null || true
    echo "D-Bus session started: $DBUS_SESSION_BUS_ADDRESS"
fi

# Clean up any stale PipeWire files
echo "Cleaning up stale audio files..."
rm -rf $XDG_RUNTIME_DIR/pipewire-* $XDG_RUNTIME_DIR/pulse 2>/dev/null || true

# Create PipeWire configuration for virtual audio
mkdir -p /etc/pipewire/pipewire.conf.d
cat > /etc/pipewire/pipewire.conf.d/10-virtual-sink.conf << 'EOF'
# Virtual sink configuration for recording
# Disable idle suspension to prevent FFmpeg audio stalls
context.properties = {
    # Disable suspend on idle - critical for recording stability
    module.suspend-on-idle = false
}
context.objects = [
    {   factory = adapter
        args = {
            factory.name     = support.null-audio-sink
            node.name        = "virtual_speaker"
            node.description = "Virtual Speaker for Recording"
            media.class      = "Audio/Sink"
            audio.position   = [ FL FR ]
            audio.rate       = 48000
            monitor.channel-volumes = true
            # Prevent node from being suspended when idle
            node.pause-on-idle = false
            session.suspend-timeout-seconds = 0
        }
    }
]
EOF

# Configure PipeWire-Pulse for compatibility
mkdir -p /etc/pipewire/pipewire-pulse.conf.d
cat > /etc/pipewire/pipewire-pulse.conf.d/10-recording.conf << 'EOF'
# Optimize for recording stability
pulse.properties = {
    server.address = [ "unix:native" ]
}
stream.properties = {
    resample.quality = 4
}
EOF

# Kill any existing audio processes
pkill -9 pipewire 2>/dev/null || true
pkill -9 wireplumber 2>/dev/null || true
sleep 0.5

# Start PipeWire
echo "Starting PipeWire audio server..."
pipewire &
PIPEWIRE_PID=$!
sleep 1

# Verify PipeWire is running
if ! kill -0 $PIPEWIRE_PID 2>/dev/null; then
    echo "ERROR: PipeWire failed to start"
else
    echo "PipeWire started (PID: $PIPEWIRE_PID)"
fi

# Start WirePlumber (session manager)
echo "Starting WirePlumber session manager..."
wireplumber &
WIREPLUMBER_PID=$!
sleep 1

if ! kill -0 $WIREPLUMBER_PID 2>/dev/null; then
    echo "WARNING: WirePlumber failed to start"
else
    echo "WirePlumber started (PID: $WIREPLUMBER_PID)"
fi

# Start PipeWire-Pulse (PulseAudio compatibility layer)
echo "Starting PipeWire-Pulse compatibility layer..."
pipewire-pulse &
PIPEWIRE_PULSE_PID=$!
sleep 1

if ! kill -0 $PIPEWIRE_PULSE_PID 2>/dev/null; then
    echo "WARNING: PipeWire-Pulse failed to start"
else
    echo "PipeWire-Pulse started (PID: $PIPEWIRE_PULSE_PID)"
fi

# Wait for audio system to be ready
AUDIO_READY=0
echo "Waiting for audio system..."
for i in $(seq 1 15); do
    sleep 0.5
    if pactl info >/dev/null 2>&1; then
        AUDIO_READY=1
        break
    fi
    echo "Waiting for audio... ($i/15)"
done

if [ "$AUDIO_READY" = "1" ]; then
    echo "Audio system ready!"

    # Check if virtual_speaker sink exists (from config), if not create it
    if ! pactl list sinks short 2>/dev/null | grep -q virtual_speaker; then
        echo "Creating virtual_speaker sink via pactl..."
        pactl load-module module-null-sink sink_name=virtual_speaker \
            sink_properties=device.description="Virtual_Speaker" \
            rate=48000 channels=2 2>/dev/null || true
        sleep 0.5
    fi

    # Set virtual_speaker as default
    pactl set-default-sink virtual_speaker 2>/dev/null || true

    # List audio devices for debugging
    echo "=== Audio Configuration (PipeWire) ==="
    echo "Sinks:"
    pactl list sinks short 2>/dev/null || true
    echo "Sources:"
    pactl list sources short 2>/dev/null || true
    echo "======================================="
else
    echo "WARNING: Audio system failed to start. Audio recording will use silent fallback."
fi

# Start VNC server if DEBUG_VNC is set (requires x11vnc to be installed)
if [ "${DEBUG_VNC:-0}" = "1" ]; then
    if ! command -v x11vnc &>/dev/null; then
        echo "WARNING: DEBUG_VNC=1 but x11vnc is not installed. Skipping VNC."
        echo "To enable VNC, add x11vnc to Dockerfile or install at runtime."
    else
        echo "Starting VNC server on port 5900 for debugging..."

        # Read resolution from environment variables, default 1920x1080
        VNC_WIDTH="${RESOLUTION_W:-1920}"
        VNC_HEIGHT="${RESOLUTION_H:-1080}"
        VNC_DEPTH="24"
        VNC_DISPLAY=":99"

        # Clean up old lock file
        if [ -f "/tmp/.X99-lock" ]; then
            echo "Removing stale X lock file..."
            rm -f /tmp/.X99-lock
        fi

        # Check if Xvfb is already running
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
fi

echo "=== Starting Application ==="
# Execute the main command
exec "$@"
