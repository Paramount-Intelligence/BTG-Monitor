#!/usr/bin/env sh
set -eu

echo "Starting BTG Project Monitor on Railway"
echo "Python: $(python --version 2>&1)"
echo "Chromium: $(chromium --version 2>&1 || true)"
echo "ChromeDriver: $(chromedriver --version 2>&1 || true)"
echo "CHROME_BIN=${CHROME_BIN:-/usr/bin/chromium}"
echo "CHROMEDRIVER_PATH=${CHROMEDRIVER_PATH:-/usr/bin/chromedriver}"
echo "PORT=${PORT:-8080}"

exec python -u btg_script.py