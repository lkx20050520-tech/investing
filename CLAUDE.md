# investing

Quantitative investing / market analysis toolkit in Python.

## Setup

```bash
pip install -e ".[dev]"
```

## Commands

- Run tests: `pytest`
- Run a single test file: `pytest tests/test_returns.py`

## Layout

- `src/investing/` — library code (src layout, installed as the `investing` package)
- `tests/` — pytest test suite, mirrors `src/investing/` module names

## Conventions

- New modules go under `src/investing/`; add a matching `tests/test_<module>.py`.
- Prefer `pandas`/`numpy` vectorized operations over manual loops for series/frame math.
