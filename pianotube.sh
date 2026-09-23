#!/bin/bash
# Lối tắt: ./pianotube.sh falling erik_satie_gymnopedie_no_1
cd "$(dirname "$0")" && exec .venv/bin/python -m pianotube "$@"
