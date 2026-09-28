#!/bin/sh
# Executed only by Castor IsolatedVerifier, in its copied /workspace candidate.
set -eu
python3 -m pip install --disable-pip-version-check --no-index --no-deps --no-cache-dir \
  --target /workspace/.t372-pytest tests/t372_wheels/*.whl
PYTHONPATH=/workspace/.t372-pytest:/workspace \
  python3 -m pytest -q -p no:cacheprovider tests/test_mathutils.py
