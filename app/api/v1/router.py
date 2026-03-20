from fastapi import APIRouter

from app.api.v1.accounts import router as accounts_router
from app.api.v1.ai import router as ai_router
from app.api.v1.auth import router as auth_router
from app.api.v1.bootstrap import router as bootstrap_router
from app.api.v1.business import router as business_router
from app.api.v1.categories import router as categories_router
from app.api.v1.counterparties import router as counterparties_router
from app.api.v1.health import router as health_router
from app.api.v1.ledger import router as ledger_router
from app.api.v1.profile import router as profile_router
from app.api.v1.profiles import router as profiles_router
from app.api.v1.receipt import router as receipt_router
from app.api.v1.summary import router as summary_router
from app.api.v1.transactions import router as transactions_router

router = APIRouter()
router.include_router(ai_router)
router.include_router(auth_router)
router.include_router(bootstrap_router)
router.include_router(business_router)
router.include_router(health_router)
router.include_router(profile_router)
router.include_router(profiles_router)
router.include_router(receipt_router)
router.include_router(summary_router)
router.include_router(transactions_router)
router.include_router(accounts_router)
router.include_router(categories_router)
router.include_router(counterparties_router)
router.include_router(ledger_router)
