# Plain Python web app. No OTEL packages here — the OTEL Operator injects the SDK at runtime.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Auto-instrumentation needs to read/write the process env; run as non-root anyway.
RUN useradd -u 10001 -m appuser
USER 10001

ENV APP_PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
