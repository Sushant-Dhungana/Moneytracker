import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.core.db import close_db_pool, get_pooled_conn, init_db_pool
from app.core.errors import register_error_handlers
from app.arthaxai.services.ai_business_vector_service import process_due_business_vector_jobs
from app.arthaxai.services.ai_personal_vector_service import process_due_personal_vector_jobs


async def _semantic_index_worker(stop_event: asyncio.Event) -> None:
    """
    Background loop for async semantic indexing.
    This keeps business and personal vector docs fresh without chat-time reindexing.
    """
    settings = get_settings()
    idle_interval_sec = 20.0
    failure_backoff_sec = 60.0
    while not stop_event.is_set():
        sleep_timeout = idle_interval_sec
        def _run_once() -> None:
            with get_pooled_conn() as conn:
                try:
                    process_due_business_vector_jobs(
                        conn,
                        settings=settings,
                        max_profiles=5,
                        max_jobs_per_profile=2,
                    )
                except Exception as exc:
                    print(f"[BusinessVectorWorker] run failed: {exc}")
                try:
                    process_due_personal_vector_jobs(
                        conn,
                        settings=settings,
                        max_profiles=5,
                        max_jobs_per_profile=2,
                    )
                except Exception as exc:
                    print(f"[PersonalVectorWorker] run failed: {exc}")

        try:
            await asyncio.to_thread(_run_once)
        except Exception as exc:  # pragma: no cover - background safety
            print(f"[SemanticIndexWorker] run failed: {exc}")
            sleep_timeout = failure_backoff_sec

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_timeout)
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db_pool()
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(_semantic_index_worker(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        close_db_pool()


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)
app.include_router(v1_router, prefix=settings.api_prefix)
