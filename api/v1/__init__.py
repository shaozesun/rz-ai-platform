from fastapi import APIRouter
from api.v1.health import router as health_router
from api.v1.auth import router as auth_router
from api.v1.admin import router as admin_router
from api.v1.rag import router as rag_router
from api.v1.chat import router as chat_router
from api.v1.risk import router as risk_router
from api.v1.video import router as video_router
from api.v1.stats import router as stats_router
from api.v1.feedback import router as feedback_router

api_v1_router = APIRouter(prefix='/api/v1')
api_v1_router.include_router(health_router, tags=['health'])
api_v1_router.include_router(auth_router, tags=['auth'])
api_v1_router.include_router(admin_router, tags=['admin'])
api_v1_router.include_router(rag_router, tags=['rag'])
api_v1_router.include_router(chat_router, tags=['chat'])
api_v1_router.include_router(risk_router, tags=['risk'])
api_v1_router.include_router(video_router, tags=['video'])
api_v1_router.include_router(stats_router, tags=['stats'])
api_v1_router.include_router(feedback_router, tags=['feedback'])
