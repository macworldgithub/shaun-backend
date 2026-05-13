from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from db import ensure_indexes, close
# from seed import bootstrap
from routers.auth import router as auth_router
from routers.clients import router as clients_router
from routers.sms import router as sms_router
from routers.admin import router as admin_router
from routers.share import router as share_router

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('app')


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    try:
        await ensure_indexes()
        # await bootstrap()
        log.info("Startup complete")
    except Exception as e:
        log.exception("Startup error: %s", e)

    yield  # app runs here

    # ---- shutdown ----
    try:
        close()
        log.info("Shutdown complete")
    except Exception as e:
        log.exception("Shutdown error: %s", e)


app = FastAPI(title='Delivery Centre API', version='1.0.0', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(auth_router)
app.include_router(clients_router)
app.include_router(sms_router)
app.include_router(admin_router)
app.include_router(share_router)


@app.get('/api/')
async def root():
    return {'service': 'Delivery Centre API', 'status': 'ok'}