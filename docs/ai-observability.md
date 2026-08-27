# AI observability

Each logical AI operation records its feature, selected provider/model, status,
latency, payload character counts, and exception class. Prompt text, model
responses, tool arguments, exception messages, credentials, and other content
are never persisted. Telemetry is best-effort and cannot fail an AI operation.

Run `PYTHONPATH=. .venv/bin/python scripts/report_ai_observability.py --hours 24`
for an aggregate operational report. A chat turn may make multiple provider
requests while using tools; it is intentionally counted once as a logical turn.
