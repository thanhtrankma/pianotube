#!/bin/bash
# Bấm đúp để mở giao diện PianoTube trong trình duyệt. Đóng cửa sổ Terminal này để tắt app.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Lần đầu chạy: đang cài môi trường Python…"
  /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m pianotube.ui
