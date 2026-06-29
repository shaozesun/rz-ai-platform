from typing import Any, Optional
from pydantic import BaseModel


class ApiResponse(BaseModel):
  ok: bool = True
  data: Any = None
  message: str = ''


class PaginatedResponse(BaseModel):
  ok: bool = True
  items: list[Any] = []
  total: int = 0
  page: int = 1
  page_size: int = 20


class ErrorResponse(BaseModel):
  ok: bool = False
  error: str
  detail: Optional[str] = None
