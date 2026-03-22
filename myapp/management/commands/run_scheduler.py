import signal
import sys
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from django.conf import settings
from django.core.management import BaseCommand
from django_apscheduler.jobstores import DjangoJobStore, register_events, register_job
import structlog

from myproject.observability import bound_log_context


logger = structlog.get_logger(__name__)

scheduler = BackgroundScheduler(
    timezone=settings.TIME_ZONE,
    jobstores={"default": DjangoJobStore()},
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    },
)


@register_job(
    scheduler,
    trigger=IntervalTrigger(minutes=1),
    id="need_to_open_notification",
    replace_existing=True,
    max_instances=1,
)
def need_to_open_notification_job():
    from dispatch.crons import need_to_open_notification
    with bound_log_context(execution_source="scheduler", job_name="need_to_open_notification"):
        need_to_open_notification()
        logger.info("scheduler_job_finished", job_name="need_to_open_notification")

@register_job(
    scheduler,
    trigger=IntervalTrigger(hours=24),
    id="check_missing_duties",
    replace_existing=True,
    max_instances=1,
)
def check_missing_duties_job():
    from dispatch.crons import check_missing_duties
    with bound_log_context(execution_source="scheduler", job_name="check_missing_duties"):
        check_missing_duties()
        logger.info("scheduler_job_finished", job_name="check_missing_duties")


class Command(BaseCommand):
    help = "Run APScheduler in this process"

    def handle(self, *args, **options):
        register_events(scheduler)
        scheduler.start()
        logger.info("scheduler_started")
        self.stdout.write(self.style.SUCCESS("APScheduler started"))

        def _graceful_exit(signum, frame):
            logger.info("scheduler_stopping", signal=signum)
            self.stdout.write("Shutting down scheduler...")
            scheduler.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGTERM, _graceful_exit)
        signal.signal(signal.SIGINT, _graceful_exit)

        while True:
            time.sleep(60)
