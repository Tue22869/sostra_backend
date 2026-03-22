import time
from datetime import UTC, datetime, timedelta

from apscheduler import events
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Avg, Count, OuterRef, Subquery
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django_apscheduler import util
from django_apscheduler.jobstores import DjangoJobStore, DjangoMemoryJobStore
from django_apscheduler.models import DjangoJob, DjangoJobExecution

from myapp.scheduler_utils import (
    cleanup_old_job_executions,
    get_job_execution_retention_days,
)


class SchedulerSuperuserOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DjangoJobAdmin(SchedulerSuperuserOnlyAdmin):
    search_fields = ["id"]
    list_display = [
        "id",
        "local_run_time",
        "last_execution_time",
        "last_status_badge",
        "total_executions",
        "average_duration",
        "recent_executions_link",
    ]
    readonly_fields = [
        "id",
        "job_state_preview",
        "next_run_time",
        "local_run_time",
        "last_execution_time",
        "last_status_badge",
        "total_executions",
        "average_duration",
        "recent_executions_link",
    ]
    fields = readonly_fields
    actions = ["run_selected_jobs"]
    ordering = ["next_run_time", "id"]

    def __init__(self, model, admin_site):
        super().__init__(model, admin_site)
        self._django_jobstore = DjangoJobStore()
        self._memory_jobstore = DjangoMemoryJobStore()
        self._jobs_scheduled = None
        self._jobs_executed = None
        self._job_execution_timeout = getattr(
            settings, "APSCHEDULER_RUN_NOW_TIMEOUT", 15
        )

    def get_queryset(self, request):
        latest_execution = DjangoJobExecution.objects.filter(
            job_id=OuterRef("pk")
        ).order_by("-run_time")
        return (
            super()
            .get_queryset(request)
            .annotate(
                execution_count=Count("djangojobexecution"),
                avg_duration=Avg("djangojobexecution__duration"),
                latest_run_time=Subquery(latest_execution.values("run_time")[:1]),
                latest_status=Subquery(latest_execution.values("status")[:1]),
            )
        )

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def local_run_time(self, obj):
        if obj.next_run_time:
            return util.get_local_dt_format(obj.next_run_time)
        return "(paused)"

    local_run_time.short_description = _("Next run time")

    def last_execution_time(self, obj):
        if not obj.latest_run_time:
            return "N/A"
        return util.get_local_dt_format(obj.latest_run_time)

    last_execution_time.short_description = _("Last execution time")

    def last_status_badge(self, obj):
        return _status_badge(obj.latest_status)

    last_status_badge.short_description = _("Last status")

    def total_executions(self, obj):
        return obj.execution_count

    total_executions.short_description = _("Executions")

    def average_duration(self, obj):
        if obj.avg_duration is None:
            return "N/A"
        return round(obj.avg_duration, 2)

    average_duration.short_description = _("Average Duration (sec)")

    def recent_executions_link(self, obj):
        url = (
            reverse("admin:django_apscheduler_djangojobexecution_changelist")
            + f"?job__id__exact={obj.pk}"
        )
        return format_html('<a href="{}">Открыть execution</a>', url)

    recent_executions_link.short_description = _("Executions")

    def job_state_preview(self, obj):
        return f"{len(obj.job_state)} bytes"

    job_state_preview.short_description = _("Job state")

    def run_selected_jobs(self, request, queryset):
        scheduler = BackgroundScheduler()
        scheduler.add_jobstore(self._memory_jobstore)
        scheduler.add_listener(self._handle_execution_event, events.EVENT_JOB_EXECUTED)
        scheduler.start()

        self._jobs_scheduled = set()
        self._jobs_executed = set()
        start_time = timezone.now()

        for item in queryset:
            django_job = self._django_jobstore.lookup_job(item.id)

            if not django_job:
                self.message_user(
                    request,
                    format_html(
                        _("Could not find job {} in the database! Skipping execution..."),
                        item.id,
                    ),
                    messages.WARNING,
                )
                continue

            scheduler.add_job(
                django_job.func_ref,
                trigger=None,
                args=django_job.args,
                kwargs=django_job.kwargs,
                id=django_job.id,
                name=django_job.name,
                misfire_grace_time=django_job.misfire_grace_time,
                coalesce=django_job.coalesce,
                max_instances=django_job.max_instances,
            )
            self._jobs_scheduled.add(django_job.id)

        while self._jobs_scheduled != self._jobs_executed:
            if timezone.now() > start_time + timedelta(
                seconds=self._job_execution_timeout
            ):
                self.message_user(
                    request,
                    format_html(
                        _(
                            "Maximum runtime of {} seconds exceeded! Pending jobs: {}"
                        ),
                        self._job_execution_timeout,
                        ",".join(self._jobs_scheduled - self._jobs_executed),
                    ),
                    messages.ERROR,
                )
                scheduler.shutdown(wait=False)
                return None

            time.sleep(0.1)

        for job_id in self._jobs_executed:
            self.message_user(request, format_html(_("Executed job '{}'!"), job_id))

        scheduler.shutdown()
        return None

    def _handle_execution_event(self, event: events.JobExecutionEvent):
        self._jobs_executed.add(event.job_id)

    run_selected_jobs.short_description = _("Run the selected django jobs")


class DjangoJobExecutionAdmin(SchedulerSuperuserOnlyAdmin):
    list_display = [
        "id",
        "job",
        "status_badge",
        "local_run_time",
        "duration_text",
        "finished_at",
        "exception_short",
    ]
    list_filter = ["job__id", "status", "run_time"]
    search_fields = ["job__id", "exception", "traceback"]
    readonly_fields = [
        "id",
        "job",
        "status_badge",
        "run_time",
        "duration",
        "finished_at",
        "exception",
        "traceback_pretty",
    ]
    fields = readonly_fields
    date_hierarchy = "run_time"
    list_select_related = ["job"]
    ordering = ["-run_time"]
    list_per_page = 100
    show_full_result_count = False
    actions = ["cleanup_old_job_executions_action"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("job")

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def status_badge(self, obj):
        return _status_badge(obj.status)

    status_badge.short_description = _("Status")

    def local_run_time(self, obj):
        return util.get_local_dt_format(obj.run_time)

    local_run_time.short_description = _("Run time")

    def duration_text(self, obj):
        return obj.duration or "N/A"

    duration_text.short_description = _("Duration (sec)")

    def finished_at(self, obj):
        if obj.finished is None:
            return "N/A"
        return timezone.localtime(
            datetime.fromtimestamp(float(obj.finished), tz=UTC)
        ).strftime("%Y-%m-%d %H:%M:%S")

    finished_at.short_description = _("Finished at")

    def exception_short(self, obj):
        if not obj.exception:
            return ""
        if len(obj.exception) <= 120:
            return obj.exception
        return f"{obj.exception[:117]}..."

    exception_short.short_description = _("Exception")

    def traceback_pretty(self, obj):
        if not obj.traceback:
            return "N/A"
        return format_html("<pre>{}</pre>", obj.traceback)

    traceback_pretty.short_description = _("Traceback")

    def cleanup_old_job_executions_action(self, request, queryset):
        deleted_count = cleanup_old_job_executions()
        retention_days = get_job_execution_retention_days()
        self.message_user(
            request,
            f"Удалено {deleted_count} execution старше {retention_days} дней.",
            messages.SUCCESS,
        )

    cleanup_old_job_executions_action.short_description = _(
        "Delete old job executions using configured retention"
    )


def _status_badge(status):
    if not status:
        return "N/A"

    color = {
        DjangoJobExecution.SUCCESS: "green",
        DjangoJobExecution.SENT: "blue",
        DjangoJobExecution.MAX_INSTANCES: "orange",
        DjangoJobExecution.MISSED: "orange",
        DjangoJobExecution.ERROR: "red",
    }.get(status, "black")
    return format_html('<span style="color: {};">{}</span>', color, status)


def register_scheduler_admin(admin_site):
    admin_site.register(DjangoJob, DjangoJobAdmin)
    admin_site.register(DjangoJobExecution, DjangoJobExecutionAdmin)
