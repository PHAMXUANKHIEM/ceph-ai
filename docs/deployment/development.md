# Ceph-AI development quickstart

Use this profile for local development or a disposable lab only.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
pytest -m 'not live'
```

Development may use SQLite and a local RabbitMQ instance. Do not expose the
development dashboard to an untrusted network, and never put production SSH
keys, provider tokens, or database credentials in the repository.

For the current test database and migration checks:

```bash
.venv/bin/python -m alembic heads
.venv/bin/python -m pytest -m 'not live' --junitxml=artifacts/pytest.xml
```

Autonomous remediation remains disabled unless the production gates and an
operator approval explicitly enable a permitted scope.
