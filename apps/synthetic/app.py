"""Tiny self-driving synthetic service.

There is NO OpenTelemetry code here on purpose: the OTEL Operator auto-instruments this
process (Flask + requests + logging) by injecting the SDK at pod start. All this app does
is expose a couple of endpoints and hammer itself in a background loop so traces, metrics,
and logs flow continuously with zero human interaction.

Signals produced:
  - traces : incoming Flask request span -> outgoing `requests` span (a 2-span trace)
  - metrics: HTTP server/client metrics from auto-instrumentation
  - logs   : the log lines below, exported via OTLP (log auto-instrumentation)
"""
import logging
import os
import random
import threading
import time

import requests
from flask import Flask, jsonify

SERVICE = os.environ.get("OTEL_SERVICE_NAME", "synthetic")
PORT = int(os.environ.get("APP_PORT", "8080"))
SELF = f"http://127.0.0.1:{PORT}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(SERVICE)

app = Flask(__name__)

# A few fake operations so traces have variety.
OPERATIONS = ["checkout", "quote", "settle", "report", "verify"]


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/work")
def work():
    op = random.choice(OPERATIONS)
    # Simulate some latency.
    time.sleep(random.uniform(0.01, 0.15))
    # Call a downstream endpoint to create a nested (client) span.
    try:
        requests.get(f"{SELF}/downstream/{op}", timeout=2)
    except requests.RequestException as exc:
        log.warning("downstream call failed op=%s err=%s", op, exc)
    log.info("handled op=%s", op)
    return jsonify(service=SERVICE, op=op)


@app.get("/downstream/<op>")
def downstream(op):
    time.sleep(random.uniform(0.005, 0.05))
    # ~1 in 12 requests fails, so error traces/logs show up in the demo.
    if random.random() < 0.08:
        log.error("simulated failure op=%s", op)
        return jsonify(op=op, error="simulated failure"), 500
    return jsonify(op=op, result="ok")


def traffic_loop():
    """Continuously call our own /work endpoint."""
    # Give the server a moment to come up.
    time.sleep(5)
    log.info("traffic generator started for service=%s", SERVICE)
    while True:
        try:
            requests.get(f"{SELF}/work", timeout=3)
        except requests.RequestException as exc:
            log.warning("self call failed err=%s", exc)
        time.sleep(random.uniform(0.5, 2.0))


if __name__ == "__main__":
    threading.Thread(target=traffic_loop, daemon=True).start()
    # threaded=True so the self-call in /work can be served concurrently.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
