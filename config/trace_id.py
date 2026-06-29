import uuid
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

trace_id_var: ContextVar[str] = ContextVar('trace_id', default='-')


class TraceIdMiddleware(BaseHTTPMiddleware):
  async def dispatch(self, request: Request, call_next):
    trace_id = request.headers.get('X-Trace-Id', uuid.uuid4().hex[:12])
    token = trace_id_var.set(trace_id)
    try:
      response = await call_next(request)
      response.headers['X-Trace-Id'] = trace_id
      return response
    finally:
      trace_id_var.reset(token)


def get_trace_id() -> str:
  return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
  trace_id_var.set(trace_id)
