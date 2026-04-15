import os

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.dublgis_crawl import dublgis_crawl_queue
from app.dublgis_crawl import router as dublgis_crawl
from app.google_auth import router as google_auth

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY"))
app.include_router(google_auth)
app.include_router(dublgis_crawl)


@app.on_event("startup")
async def startup() -> None:
    await dublgis_crawl_queue.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await dublgis_crawl_queue.stop()


@app.get("/")
async def root():
    return {"message": "Hello World"}
