from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging

from db import ensure_indexes, close
from seed import bootstrap
from routers.auth import router as auth_router
from routers.clients import router as clients_router
from routers.sms import router as sms_router
from routers.admin import router as admin_router
from routers.share import router as share_router
from routers.contractor import router as contractor_router
from routers.contractor_users import router as contractor_users_router
from routers.virtualyard import router as virtualyard_router
from routers.staff import router as staff_router
from services.notifications import scheduled_workflow_worker

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('app')


@asynccontextmanager
async def lifespan(app: FastAPI):
    workflow_task = None
    integration_task = None
    # ---- startup ----
    try:
        await ensure_indexes()
        # await bootstrap()
        log.info("Startup complete")
        workflow_task = asyncio.create_task(scheduled_workflow_worker())
        from services.contractor_integration import run_integration_worker
        integration_task = asyncio.create_task(run_integration_worker())
    except Exception as e:
        log.exception("Startup error: %s", e)

    yield  # app runs here

    # ---- shutdown ----
    try:
        if integration_task:
            integration_task.cancel()
            try:
                await integration_task
            except asyncio.CancelledError:
                pass
        if workflow_task:
            workflow_task.cancel()
            try:
                await workflow_task
            except asyncio.CancelledError:
                pass
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
app.include_router(contractor_router)
app.include_router(contractor_users_router)
app.include_router(virtualyard_router)
app.include_router(staff_router)


@app.get('/api/')
async def root():
    return {'service': 'Delivery Centre API', 'status': 'okk'}
