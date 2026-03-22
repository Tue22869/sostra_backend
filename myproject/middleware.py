import time

import structlog
from rest_framework_simplejwt.authentication import JWTAuthentication

from myproject.observability import new_request_id


logger = structlog.get_logger(__name__)


def _get_client_ip(request) -> str | None:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _hydrate_user_from_jwt(request):
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False):
            return user

        try:
            jwt_auth = JWTAuthentication()
            header = jwt_auth.get_header(request)
            if header is None:
                return user
            raw_token = jwt_auth.get_raw_token(header)
            if raw_token is None:
                return user
            validated_token = jwt_auth.get_validated_token(raw_token)
            jwt_user = jwt_auth.get_user(validated_token)
        except Exception:
            return user

        request.user = jwt_user
        request._cached_user = jwt_user
        return jwt_user

    def __call__(self, request):
        structlog.contextvars.clear_contextvars()

        request_id = request.META.get("HTTP_X_REQUEST_ID") or new_request_id()
        request.request_id = request_id

        context = {
            "request_id": request_id,
            "http_method": request.method,
            "http_path": request.path,
            "remote_addr": _get_client_ip(request),
        }

        user = self._hydrate_user_from_jwt(request)
        if getattr(user, "is_authenticated", False):
            context["user_id"] = user.pk
            context["username"] = user.get_username()

        structlog.contextvars.bind_contextvars(**context)

        started_at = time.perf_counter()
        logger.info("http_request_started")

        try:
            response = self.get_response(request)
        except Exception:
            logger.exception(
                "http_request_failed",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()
            raise

        response["X-Request-ID"] = request_id
        logger.info(
            "http_request_finished",
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
        structlog.contextvars.clear_contextvars()
        return response
