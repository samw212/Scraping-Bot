#!/bin/sh
# HEADFUL=1 runs Chromium with a real (virtual) display under Xvfb. Headful
# Chromium avoids a whole class of headless-only fingerprint differences.
set -e
export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"   # lets examples.* extractors import
if [ "${HEADFUL:-0}" = "1" ]; then
  exec xvfb-run --auto-servernum --server-args="-screen 0 ${XVFB_SCREEN:-2560x1440x24}" \
    scrapebot "$@" --set browser.headless=false
fi
exec scrapebot "$@"
