import os
from datetime import timedelta

import structlog
from django.utils import timezone
from django_apscheduler.models import DjangoJobExecution


DEFAULT_JOB_EXECUTION_RETENTION_DAYS = 30
JOB_EXECUTION_RETENTION_ENV = "APSCHEDULER_EXECUTION_RETENTION_DAYS"

logger = structlog.get_logger(__name__)


def get_job_execution_retention_days() -> int:
    raw_value = os.getenv(
        JOB_EXECUTION_RETENTION_ENV,
        str(DEFAULT_JOB_EXECUTION_RETENTION_DAYS),
    )
    try:
        retention_days = int(raw_value)
    except (TypeError, ValueError):
        retention_days = DEFAULT_JOB_EXECUTION_RETENTION_DAYS

    return max(1, retention_days)


def cleanup_old_job_executions(*, retention_days: int | None = None, now_value=None) -> int:
    retention_days = retention_days or get_job_execution_retention_days()
    now_value = now_value or timezone.now()
    cutoff = now_value - timedelta(days=retention_days)

    queryset = DjangoJobExecution.objects.filter(run_time__lte=cutoff)
    deleted_execution_count = queryset.count()
    if deleted_execution_count:
        queryset.delete()

    logger.info(
        "scheduler_job_executions_cleanup_finished",
        retention_days=retention_days,
        deleted_execution_count=deleted_execution_count,
        cutoff=cutoff.isoformat(),
    )
    return deleted_execution_count
