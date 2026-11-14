#!/usr/bin/env bash
# Regenerate rig/SHA256SUMS (paths relative to rig/). MUST be run before
# every commit that changes rig/agent.py or rig/qubic_parse.py.
set -euo pipefail
cd "$(dirname "$0")/../rig"
sha256sum agent.py qubic_parse.py > SHA256SUMS
echo "wrote rig/SHA256SUMS:"
cat SHA256SUMS
