import enum
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from django.db import models
from django.db.models.fields.files import FieldFile
from django.utils import timezone
import structlog


def configure_structlog() -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
    ]

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class DailyStructuredFileHandler(logging.Handler):
    terminator = "\n"

    def __init__(
        self,
        log_dir: str,
        filename_prefix: str = "application",
        retention_days: int = 14,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self.log_dir = Path(log_dir)
        self.filename_prefix = filename_prefix
        self.retention_days = int(retention_days)
        self.encoding = encoding
        self._stream = None
        self._current_date: date | None = None
        self._filename_regex = re.compile(
            rf"^{re.escape(self.filename_prefix)}-(\d{{4}}-\d{{2}}-\d{{2}})\.log$"
        )
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _today(self) -> date:
        return timezone.localdate()

    def _build_path(self, log_date: date) -> Path:
        return self.log_dir / f"{self.filename_prefix}-{log_date.isoformat()}.log"

    def _cleanup_old_files(self, current_date: date) -> None:
        cutoff_date = current_date - timedelta(days=self.retention_days)
        for path in self.log_dir.glob(f"{self.filename_prefix}-*.log"):
            match = self._filename_regex.match(path.name)
            if not match:
                continue
            try:
                file_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if file_date < cutoff_date:
                path.unlink(missing_ok=True)

    def _ensure_stream(self) -> None:
        current_date = self._today()
        if self._stream is not None and self._current_date == current_date:
            return

        if self._stream is not None:
            self._stream.close()

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stream = open(
            self._build_path(current_date),
            mode="a",
            encoding=self.encoding,
            buffering=1,
        )
        self._current_date = current_date
        self._cleanup_old_files(current_date)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            if self._stream is None:
                return
            self._stream.write(self.format(record) + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        if self._stream is not None:
            self._stream.flush()

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def build_logging_config(
    level: str = "INFO",
    *,
    log_dir: str | os.PathLike[str] = "logs",
    filename_prefix: str = "application",
    retention_days: int = 14,
) -> dict[str, Any]:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    foreign_pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
    ]

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processor": structlog.processors.JSONRenderer(
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                "foreign_pre_chain": foreign_pre_chain,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
            },
            "daily_file": {
                "()": "myproject.observability.DailyStructuredFileHandler",
                "formatter": "json",
                "log_dir": str(log_dir),
                "filename_prefix": filename_prefix,
                "retention_days": retention_days,
            },
        },
        "root": {
            "handlers": ["console", "daily_file"],
            "level": level,
        },
        "loggers": {
            "django": {
                "handlers": ["console", "daily_file"],
                "level": level,
                "propagate": False,
            },
            "django.db.backends": {
                "handlers": ["console", "daily_file"],
                "level": "WARNING",
                "propagate": False,
            },
            "apscheduler": {
                "handlers": ["console", "daily_file"],
                "level": level,
                "propagate": False,
            },
        },
    }


def new_request_id() -> str:
    return uuid.uuid4().hex


def capture_log_context() -> dict[str, Any]:
    context = dict(structlog.contextvars.get_contextvars())
    context.setdefault("request_id", new_request_id())
    return context


@contextmanager
def bound_log_context(**context: Any):
    clean_context = {key: value for key, value in context.items() if value is not None}
    clean_context.setdefault("request_id", new_request_id())
    with structlog.contextvars.bound_contextvars(**clean_context):
        yield clean_context


def serialize_for_log(value: Any) -> Any:
    if isinstance(value, models.Model):
        return {
            "id": value.pk,
            "model": value._meta.label_lower,
            "repr": str(value),
        }
    if isinstance(value, FieldFile):
        return value.name or None
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): serialize_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [serialize_for_log(item) for item in value]
    return value


def model_snapshot(instance: models.Model, include_m2m: bool = False) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}

    for field in instance._meta.concrete_fields:
        field_name = field.attname if field.is_relation else field.name
        snapshot[field_name] = serialize_for_log(getattr(instance, field.attname))
        if field.choices:
            snapshot[f"{field.name}_display"] = serialize_for_log(
                getattr(instance, f"get_{field.name}_display")()
            )

    if include_m2m and instance.pk:
        for field in instance._meta.many_to_many:
            snapshot[field.name] = list(
                getattr(instance, field.name).values_list("pk", flat=True)
            )

    return snapshot


def diff_snapshots(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    before = before or {}
    after = after or {}
    changes: dict[str, dict[str, Any]] = {}

    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changes[key] = {
                "old": before.get(key),
                "new": after.get(key),
            }

    return changes


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
