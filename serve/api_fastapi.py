from fastapi import FastAPI

app = FastAPI(title="Weather Wizard", version="0.0.1")


@app.get("/")
def root():
    return {"message": "Weather Wizard base running"}


@app.get("/health")
def health():
    return {"status": "ok"}
