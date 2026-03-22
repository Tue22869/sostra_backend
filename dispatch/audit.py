from functools import partial

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save

from dispatch.models import (
    AudioMessage,
    Duty,
    DutyAction,
    DutyPoint,
    DutyRole,
    ExploitationRole,
    Incident,
    IncidentMessage,
    PhotoMessage,
    TextMessage,
    VideoMessage,
    WeekendDutyAssignment,
)
from myproject.observability import diff_snapshots, get_logger, model_snapshot


logger = get_logger(__name__)

_AUDIT_SIGNAL_REGISTERED = False
_AUDITED_MODELS = (
    DutyRole,
    ExploitationRole,
    DutyPoint,
    Duty,
    WeekendDutyAssignment,
    DutyAction,
    Incident,
    IncidentMessage,
    TextMessage,
    PhotoMessage,
    VideoMessage,
    AudioMessage,
)


def _object_context(instance):
    return {
        "model": instance._meta.label_lower,
        "object_id": instance.pk,
        "object_repr": str(instance),
    }


def _cache_previous_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._audit_previous_snapshot = None
        return

    previous_instance = sender.objects.filter(pk=instance.pk).first()
    instance._audit_previous_snapshot = (
        model_snapshot(previous_instance) if previous_instance else None
    )


def _log_saved(sender, instance, created, **kwargs):
    before = getattr(instance, "_audit_previous_snapshot", None)
    after = model_snapshot(instance)
    changes = diff_snapshots(before, after)

    logger.info(
        "dispatch_model_created" if created else "dispatch_model_updated",
        **_object_context(instance),
        before=before,
        after=after,
        changes=changes,
    )

    if hasattr(instance, "_audit_previous_snapshot"):
        delattr(instance, "_audit_previous_snapshot")


def _log_deleted(sender, instance, **kwargs):
    logger.warning(
        "dispatch_model_deleted",
        **_object_context(instance),
        snapshot=model_snapshot(instance),
    )


def _log_m2m_change(field_name, sender, instance, action, reverse, pk_set, **kwargs):
    if action not in {"post_add", "post_remove", "post_clear"}:
        return

    logger.info(
        "dispatch_model_m2m_changed",
        **_object_context(instance),
        field=field_name,
        action=action,
        reverse=reverse,
        related_ids=sorted(pk_set) if pk_set else [],
        current_ids=list(getattr(instance, field_name).values_list("pk", flat=True)),
    )


def register_dispatch_audit_signals():
    global _AUDIT_SIGNAL_REGISTERED
    if _AUDIT_SIGNAL_REGISTERED:
        return

    for model in _AUDITED_MODELS:
        model_key = model._meta.label_lower.replace(".", "_")
        pre_save.connect(
            _cache_previous_state,
            sender=model,
            dispatch_uid=f"{model_key}_audit_pre_save",
        )
        post_save.connect(
            _log_saved,
            sender=model,
            dispatch_uid=f"{model_key}_audit_post_save",
        )
        post_delete.connect(
            _log_deleted,
            sender=model,
            dispatch_uid=f"{model_key}_audit_post_delete",
        )

    m2m_changed.connect(
        partial(_log_m2m_change, "members"),
        sender=ExploitationRole.members.through,
        dispatch_uid="dispatch_exploitation_role_members_audit_m2m",
    )
    m2m_changed.connect(
        partial(_log_m2m_change, "admins"),
        sender=DutyPoint.admins.through,
        dispatch_uid="dispatch_duty_point_admins_audit_m2m",
    )

    _AUDIT_SIGNAL_REGISTERED = True
