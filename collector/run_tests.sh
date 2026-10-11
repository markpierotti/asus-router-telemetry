#!/bin/bash
# Run the offline validation suite. Contacts nothing.
set -eu
cd "$(dirname "$0")"
chmod +x tests/fake_ssh.sh 2>/dev/null || true
exec python3 -m unittest discover -s tests -p 'test_*.py' -v "$@"
