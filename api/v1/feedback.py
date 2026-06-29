"""用户反馈与建议"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from config.mongodb_conn import mongodb_manager
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class FeedbackRequest(BaseModel):
  content: str = Field(..., min_length=1, max_length=2000, description='反馈内容')


@router.post('/feedback')
async def submit_feedback(request: Request, body: FeedbackRequest):
  user_id = request.state.user_id
  user = request.state.current_user or {}
  phone = user.get('phone', '')

  doc = {
    'feedback_id': uuid.uuid4().hex,
    'user_id': user_id,
    'phone': phone,
    'content': body.content.strip(),
    'created_at': datetime.utcnow(),
  }
  await mongodb_manager.db.feedbacks.insert_one(doc)
  logger.info(f'用户反馈: user_id={user_id}, len={len(body.content)}')

  return {'ok': True}
