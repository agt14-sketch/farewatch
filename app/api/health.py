from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def index():
    return {"message": "farewatch api up", "docs": "/docs", "health": "/healthz"}

@app.get("/healthz")
def healthz():
    return {"ok": True}