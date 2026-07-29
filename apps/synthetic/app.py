"""Tiny self-driving synthetic service.

Signals produced (simulates a classic cluster mid-migration to OTLP):

  traces  : OTLP spans via auto-instrumentation (Flask + requests HTTP)
            + Jaeger-format business spans via jaeger-client (OpenTracing API)
              (models a service that hasn't migrated its business tracing to OTLP)
  metrics : Prometheus scrape endpoint on :9090 via prometheus_client
            (no OTLP metric export — the collector scrapes instead)
  logs    : stdout (always) + JSON lines to /var/log/app/app.log
            (file is tailed by the filelog sidecar collector in the same pod)

jaeger-client uses the OpenTracing API (opentracing.*), which is a completely
different package from opentelemetry.*. No conflict with the OTEL SDK injected
by the Operator — they are different global registries, different wire formats.
"""
import json
import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, start_http_server

SERVICE = os.environ.get("OTEL_SERVICE_NAME", "synthetic")
PORT = int(os.environ.get("APP_PORT", "8080"))
PROM_PORT = int(os.environ.get("PROMETHEUS_PORT", "9090"))
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/app/app.log")
JAEGER_ENDPOINT = os.environ.get(
    "JAEGER_ENDPOINT",
    "http://gateway-collector.observability.svc.cluster.local:14268/api/traces",
)
SELF = f"http://127.0.0.1:{PORT}"

# ── Prometheus metrics (classic scrape pattern) ───────────────────────────────
ops_total = Counter(
    "synthetic_ops_total",
    "Total synthetic operation invocations",
    ["service", "op", "status"],
)
op_duration = Histogram(
    "synthetic_op_duration_seconds",
    "Latency of synthetic operations in seconds",
    ["service", "op"],
)

# ── Logging: stdout (existing) + JSON file (classic file-logger pattern) ─────
class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": record.name,
            "msg": record.getMessage(),
        })


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(SERVICE)

try:
    _fh = logging.FileHandler(LOG_FILE)
    _fh.setFormatter(_JsonFormatter())
    log.addHandler(_fh)
except OSError:
    log.warning("could not open log file %s — file logging disabled", LOG_FILE)

# ── Jaeger tracer (OpenTracing API via jaeger-client + custom HTTP reporter) ───
# jaeger-client implements the OpenTracing API (opentracing.*), completely
# separate from opentelemetry.*. No conflict with the OTel Operator's injected
# SDK. The built-in Reporter only speaks UDP; we bypass it with a minimal
# Thrift-over-HTTP reporter that POSTs directly to the gateway collector.
_jaeger_tracer = None


class _ThriftHTTPReporter:
    """Batches spans and POSTs them as Thrift binary to the Jaeger HTTP collector."""

    def __init__(self, endpoint, service_name):
        from jaeger_client import thrift as jt
        self._endpoint = endpoint
        self._process = jt.make_process(service_name, [], 0)
        self._spans: list = []
        self._lock = threading.Lock()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def report_span(self, span) -> None:
        with self._lock:
            self._spans.append(span)

    def set_process(self, service_name, tags, max_length) -> None:
        from jaeger_client import thrift as jt
        self._process = jt.make_process(service_name, tags, max_length)

    def close(self):
        self._flush()

    def _flush_loop(self):
        while True:
            time.sleep(1)
            self._flush()

    def _flush(self):
        with self._lock:
            spans, self._spans = self._spans, []
        if not spans:
            return
        from jaeger_client import thrift as jt
        from thrift.protocol import TBinaryProtocol
        from thrift.transport import TTransport
        batch = jt.make_jaeger_batch(spans, self._process)
        buf = TTransport.TMemoryBuffer()
        batch.write(TBinaryProtocol.TBinaryProtocol(buf))
        try:
            requests.post(
                self._endpoint,
                data=buf.getvalue(),
                headers={"Content-Type": "application/x-thrift"},
                timeout=5,
            )
        except requests.RequestException as exc:
            log.debug("jaeger flush failed: %s", exc)


def _init_jaeger_tracer():
    global _jaeger_tracer
    import opentracing
    from jaeger_client import ConstSampler
    from jaeger_client.config import Config

    reporter = _ThriftHTTPReporter(JAEGER_ENDPOINT, SERVICE)
    sampler = ConstSampler(decision=True)
    config = Config(config={}, service_name=SERVICE, validate=False)
    _jaeger_tracer = config.create_tracer(reporter=reporter, sampler=sampler)
    opentracing.tracer = _jaeger_tracer
    log.info("jaeger tracer initialised endpoint=%s", JAEGER_ENDPOINT)


@contextmanager
def _jaeger_span(operation):
    if _jaeger_tracer is None:
        yield None
        return
    with _jaeger_tracer.start_active_span(f"business.{operation}") as scope:
        scope.span.set_tag("op.name", operation)
        scope.span.set_tag("service.name", SERVICE)
        yield scope.span


# ── Flask application ─────────────────────────────────────────────────────────
app = Flask(__name__)

OPERATIONS = ["checkout", "quote", "settle", "report", "verify"]


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/work")
def work():
    op = random.choice(OPERATIONS)
    with op_duration.labels(service=SERVICE, op=op).time():
        with _jaeger_span(op):
            time.sleep(random.uniform(0.01, 0.15))
            status = "ok"
            try:
                requests.get(f"{SELF}/downstream/{op}", timeout=2)
            except requests.RequestException as exc:
                log.warning("downstream call failed op=%s err=%s", op, exc)
                status = "error"
            log.info("handled op=%s", op)
            ops_total.labels(service=SERVICE, op=op, status=status).inc()
    return jsonify(service=SERVICE, op=op)


@app.get("/downstream/<op>")
def downstream(op):
    time.sleep(random.uniform(0.005, 0.05))
    if random.random() < 0.08:
        log.error("simulated failure op=%s", op)
        return jsonify(op=op, error="simulated failure"), 500
    return jsonify(op=op, result="ok")


def traffic_loop():
    """Continuously call our own /work endpoint."""
    time.sleep(5)
    log.info("traffic generator started for service=%s", SERVICE)
    while True:
        try:
            requests.get(f"{SELF}/work", timeout=3)
        except requests.RequestException as exc:
            log.warning("self call failed err=%s", exc)
        time.sleep(random.uniform(0.5, 2.0))


if __name__ == "__main__":
    start_http_server(PROM_PORT)
    try:
        _init_jaeger_tracer()
    except Exception as exc:
        log.warning("jaeger tracer disabled — %s", exc)
    threading.Thread(target=traffic_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
