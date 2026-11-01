# Universal Publishing Platform container entrypoint (specification §15).
#
# `exec` makes uvicorn PID 1, so the container runtime's SIGTERM/SIGINT reach
# it directly. Uvicorn stops accepting new requests, then runs the FastAPI
# lifespan shutdown, which stops the scheduler and publishing engine and
# drains the in-flight engine tick before the process exits (step-5 wiring).
#
# Give the runtime a stop timeout (Compose `stop_grace_period`, or
# `docker stop -t`) larger than the graceful-shutdown bound below so the
# drain is not cut short by SIGKILL.
set -eu

: "${APP_PORT:=8000}"
: "${UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN:=30}"

exec uvicorn \
    publisher.main:app \
    --host 0.0.0.0 \
    --port "$APP_PORT" \
    --timeout-graceful-shutdown "$UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"
