#!/bin/bash
# AI key-rotating proxy - start/stop script (no Docker needed)
# Usage: ./start.sh [start|stop|restart|status|logs]
set -eu
cd "$(dirname "$0")"

VENV=".venv"
PIDFILE=".proxy.pid"
LOGFILE="proxy.log"

get_port() {
  local p=""
  if [ -f .env ]; then
    p=$(grep -E '^PORT=' .env | cut -d= -f2 | tr -d ' ' || true)
  fi
  echo "${p:-8000}"
}

is_running() {
  if [ -f "$PIDFILE" ]; then
    local pid
    pid=$(cat "$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then return 0; fi
    rm -f "$PIDFILE"
  fi
  return 1
}

do_start() {
  if is_running; then
    echo "Already running (pid $(cat $PIDFILE)). Use ./start.sh restart"
    return 0
  fi
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example - edit it (API_KEYS, TELEGRAM_*) then run again."
    exit 1
  fi
  if [ ! -d "$VENV" ]; then
    echo "-- creating venv --"
    python3 -m venv "$VENV"
  fi
  echo "-- installing deps --"
  "$VENV/bin/pip" install -q -r requirements.txt

  local port
  port=$(get_port)
  echo "-- starting on port $port (log: $LOGFILE) --"
  nohup "$VENV/bin/python" -m uvicorn app:app --host 0.0.0.0 --port "$port" >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 3
  if is_running; then
    echo "Running (pid $(cat $PIDFILE)). Health:"
    curl -s "http://127.0.0.1:$port/health" || echo "(health check failed - see $LOGFILE)"
    echo ""
  else
    echo "Failed to start - see $LOGFILE"
    exit 1
  fi
}

do_stop() {
  if is_running; then
    kill "$(cat $PIDFILE)" && rm -f "$PIDFILE"
    echo "Stopped."
  else
    pkill -f "uvicorn app:app" 2>/dev/null || true
    echo "Not running."
  fi
}

case "${1:-start}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)
    if is_running; then
      echo "Running (pid $(cat $PIDFILE))"
      curl -s "http://127.0.0.1:$(get_port)/health" || true
      echo ""
    else
      echo "Stopped."
    fi
    ;;
  logs) tail -f "$LOGFILE" ;;
  *) echo "Usage: $0 [start|stop|restart|status|logs]"; exit 1 ;;
esac
