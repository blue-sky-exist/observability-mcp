# Contributing

Thank you for helping improve Observability MCP.

## Development setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
pip install -r requirements-dev.txt
```

Before opening a pull request, run:

```bash
python -m ruff check .
python -m pytest -q
```

Tests must use mocked backends and must not depend on private Elasticsearch,
SkyWalking, Prometheus, or VictoriaMetrics deployments.

## Pull requests

- Keep changes focused and preserve existing tool contracts unless the change is explicitly
  documented as breaking.
- Add tests for new configuration, backend responses, pagination, and error handling.
- Update `README.md` and `.env.example` when behavior changes.
- Use placeholder hostnames and credentials in committed examples.

## Security and private data

Never commit credentials, internal hostnames, private log contents, trace IDs, PromQL results,
or a populated `.env` file. Follow `SECURITY.md` when reporting a vulnerability.
