#!/bin/sh
# Container entrypoint: `serve` (default) or `worker`, or any other scriptorium subcommand.
# exec replaces the shell, so scriptorium runs as PID 1 and receives SIGTERM directly.
set -eu

exec scriptorium "$@"
