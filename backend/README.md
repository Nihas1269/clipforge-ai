# ClipForge AI Backend

FastAPI backend for ClipForge AI.

## Requirements

- Python 3.10 or newer

## Setup

Run all commands from the `backend/` directory.

Create and activate a virtual environment:

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Running the server

```bash
uvicorn app.main:app --reload
```

The API is served at `http://127.0.0.1:8000`. Interactive docs are available at `http://127.0.0.1:8000/docs`.

## Endpoints

| Method | Path      | Response             |
| ------ | --------- | -------------------- |
| GET    | `/health` | `{"status": "ok"}`   |

## Running the tests

```bash
pytest
```
