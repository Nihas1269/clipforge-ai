from fastapi import FastAPI

app = FastAPI(title="ClipForge AI")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
