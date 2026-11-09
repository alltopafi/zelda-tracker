#!/bin/sh
# Starts the virtual display and noVNC, then the tracker.
# Any command other than "run" (check-once, pause, healthcheck, ...) skips the
# display stack unless it needs a browser.
set -eu

cmd="${1:-run}"
DISPLAY="${DISPLAY:-:99}"
export DISPLAY
display_num="${DISPLAY#:}"

start_display() {
    if [ -S "/tmp/.X11-unix/X${display_num}" ]; then
        return 0
    fi
    rm -f "/tmp/.X${display_num}-lock"
    Xvfb "$DISPLAY" -screen 0 "${SCREEN_GEOMETRY:-1366x850x24}" -nolisten tcp >/tmp/xvfb.log 2>&1 &
    i=0
    while [ ! -S "/tmp/.X11-unix/X${display_num}" ]; do
        i=$((i + 1))
        if [ "$i" -gt 100 ]; then
            echo "entrypoint: Xvfb did not start" >&2
            cat /tmp/xvfb.log >&2 || true
            exit 1
        fi
        sleep 0.1
    done
}

start_vnc() {
    fluxbox >/tmp/fluxbox.log 2>&1 &
    # x11vnc listens on localhost only; noVNC (websockify) is the one way in,
    # and that port is reached with `kubectl port-forward`, never a Service.
    if [ -n "${VNC_PASSWORD:-}" ]; then
        x11vnc -display "$DISPLAY" -localhost -forever -shared -quiet -rfbport 5900 \
            -passwd "$VNC_PASSWORD" >/tmp/x11vnc.log 2>&1 &
    else
        x11vnc -display "$DISPLAY" -localhost -forever -shared -quiet -rfbport 5900 \
            -nopw >/tmp/x11vnc.log 2>&1 &
    fi
    websockify --web /usr/share/novnc "${NOVNC_PORT:-6080}" localhost:5900 >/tmp/novnc.log 2>&1 &
}

case "$cmd" in
    run)
        start_display
        start_vnc
        ;;
    check-once)
        start_display
        ;;
esac

cd /app
exec python3 -m tracker "$@"
