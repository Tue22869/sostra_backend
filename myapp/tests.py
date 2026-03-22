import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_apscheduler.models import DjangoJob, DjangoJobExecution

from myapp.management.commands.run_scheduler import scheduler
from myapp.scheduler_utils import cleanup_old_job_executions
from myproject.observability import DailyStructuredFileHandler


class DailyStructuredFileHandlerTests(TestCase):
    def test_daily_file_handler_writes_current_day_file_and_cleans_old_ones(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            handler = DailyStructuredFileHandler(
                log_dir=log_dir,
                filename_prefix="application",
                retention_days=14,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler._today = lambda: date(2026, 3, 22)

            (log_dir / "application-2026-03-01.log").write_text("old\n", encoding="utf-8")
            (log_dir / "application-2026-03-08.log").write_text("keep\n", encoding="utf-8")

            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="hello world",
                args=(),
                exc_info=None,
            )
            handler.emit(record)
            handler.close()

            current_log = log_dir / "application-2026-03-22.log"

            self.assertTrue(current_log.exists())
            self.assertIn("hello world", current_log.read_text(encoding="utf-8"))
            self.assertFalse((log_dir / "application-2026-03-01.log").exists())
            self.assertTrue((log_dir / "application-2026-03-08.log").exists())


class SchedulerAdminTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="scheduler-admin",
            password="pass",
            email="scheduler-admin@example.com",
        )
        self.client.force_login(self.superuser)

    def test_superuser_can_open_scheduler_execution_admin(self):
        job = DjangoJob.objects.create(id="need_to_open_notification", job_state=b"state")
        DjangoJobExecution.objects.create(
            job=job,
            status=DjangoJobExecution.SUCCESS,
            run_time=timezone.now(),
            duration=1.25,
            finished=timezone.now().timestamp(),
        )

        response = self.client.get(
            reverse("admin:django_apscheduler_djangojobexecution_changelist")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "need_to_open_notification")

    def test_admin_cleanup_action_deletes_old_scheduler_executions(self):
        job = DjangoJob.objects.create(id="cleanup-test-job", job_state=b"state")
        old_execution = DjangoJobExecution.objects.create(
            job=job,
            status=DjangoJobExecution.SUCCESS,
            run_time=timezone.now() - timedelta(days=30),
            duration=1.0,
            finished=(timezone.now() - timedelta(days=30)).timestamp(),
        )
        recent_execution = DjangoJobExecution.objects.create(
            job=job,
            status=DjangoJobExecution.SUCCESS,
            run_time=timezone.now() - timedelta(days=1),
            duration=1.0,
            finished=(timezone.now() - timedelta(days=1)).timestamp(),
        )

        response = self.client.post(
            reverse("admin:django_apscheduler_djangojobexecution_changelist"),
            data={
                "action": "cleanup_old_job_executions_action",
                "_selected_action": [old_execution.pk, recent_execution.pk],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DjangoJobExecution.objects.filter(pk=old_execution.pk).exists())
        self.assertTrue(DjangoJobExecution.objects.filter(pk=recent_execution.pk).exists())

    def test_cleanup_helper_deletes_only_old_scheduler_executions(self):
        job = DjangoJob.objects.create(id="cleanup-helper-job", job_state=b"state")
        old_execution = DjangoJobExecution.objects.create(
            job=job,
            status=DjangoJobExecution.SUCCESS,
            run_time=timezone.now() - timedelta(days=20),
            duration=1.0,
            finished=(timezone.now() - timedelta(days=20)).timestamp(),
        )
        recent_execution = DjangoJobExecution.objects.create(
            job=job,
            status=DjangoJobExecution.SUCCESS,
            run_time=timezone.now() - timedelta(days=2),
            duration=1.0,
            finished=(timezone.now() - timedelta(days=2)).timestamp(),
        )

        deleted_count = cleanup_old_job_executions(
            retention_days=14,
            now_value=timezone.now(),
        )

        self.assertEqual(deleted_count, 1)
        self.assertFalse(DjangoJobExecution.objects.filter(pk=old_execution.pk).exists())
        self.assertTrue(DjangoJobExecution.objects.filter(pk=recent_execution.pk).exists())

    def test_scheduler_registers_cleanup_job(self):
        cleanup_job = scheduler.get_job("cleanup_old_job_executions")

        self.assertIsNotNone(cleanup_job)
