#!/bin/bash
set -eu
umask 077

# All audio processes run as the unprivileged application user.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/meeting-recorder-runtime}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME/pipewire/pipewire.conf.d"     "$XDG_CONFIG_HOME/pipewire/pipewire-pulse.conf.d"
chmod 700 "$XDG_RUNTIME_DIR"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/session-bus"
rm -f "$XDG_RUNTIME_DIR/session-bus"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork
rm -f "$XDG_RUNTIME_DIR/pipewire-0" "$XDG_RUNTIME_DIR/pipewire-0.lock"
rm -f "$XDG_RUNTIME_DIR/pulse/native"

cat > "$XDG_CONFIG_HOME/pipewire/pipewire.conf.d/10-virtual-sink.conf" <<'EOF'
context.properties = { module.suspend-on-idle = false }
context.objects = [
    { factory = adapter
      args = {
        factory.name = support.null-audio-sink
        node.name = "virtual_speaker"
        node.description = "Virtual Speaker for Recording"
        media.class = "Audio/Sink"
        audio.position = [ FL FR ]
        audio.rate = 48000
        monitor.channel-volumes = true
        node.pause-on-idle = false
        session.suspend-timeout-seconds = 0
      }
    }
]
EOF
cat > "$XDG_CONFIG_HOME/pipewire/pipewire-pulse.conf.d/10-recording.conf" <<'EOF'
pulse.properties = { server.address = [ "unix:native" ] }
stream.properties = { resample.quality = 4 }
EOF
pipewire &
wireplumber &
pipewire-pulse &
audio_ready=0
for attempt in $(seq 1 30); do
    if pactl info >/dev/null 2>&1; then
        audio_ready=1
        break
    fi
    sleep 0.5
done
if [ "$audio_ready" != 1 ]; then
    echo "ERROR: recording audio server failed to start" >&2
    exit 1
fi
pactl set-default-sink virtual_speaker >/dev/null 2>&1 || true

# Attach to an actual per-job display when it exists; do not reserve a capture slot.
if [ "${DEBUG_VNC:-0}" = 1 ]; then
    debug_display="${DEBUG_VNC_DISPLAY:-100}"
    if ! [[ "$debug_display" =~ ^[0-9]+$ ]]; then
        echo "ERROR: DEBUG_VNC_DISPLAY must be a display number" >&2
        exit 1
    fi
    (
        while true; do
            if [ -S "/tmp/.X11-unix/X$debug_display" ]; then
                x11vnc -display ":$debug_display" -forever -nopw -shared                     -rfbport 5900 -o /tmp/x11vnc.log || true
            fi
            sleep 2
        done
    ) &
fi
exec "$@"
