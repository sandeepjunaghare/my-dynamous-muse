# Local Prospect Engine

Internal GTM tool for Compumatrice. A brief (vertical · ICP band · geography) goes in; a qualified,
route-clustered prospect list comes out in HubSpot with the three-touch cadence already scheduled.

See `CLAUDE.md` for the ground rules, `docs/` for the PRD, architecture and tickets.

## Development

```bash
uv sync                                  # install
uv run uvicorn app.main:app --reload     # run
uv run pytest                            # test
uv run mypy . && uv run pyright          # types (both, strict, zero suppressions)
uv run ruff check . && uv run ruff format --check .
```
