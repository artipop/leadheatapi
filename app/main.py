import os

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.google_auth import router as google_auth

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY"))
app.include_router(google_auth)


@app.get("/")
async def root():
    return {"message": "Hello World"}
