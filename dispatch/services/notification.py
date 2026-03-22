import os
from concurrent.futures import ThreadPoolExecutor
from itertools import chain

import structlog
from django.db import close_old_connections

from dispatch.services.access import dispatch_admins
from dispatch.services.duties import get_duty_point_participants
from myapp.utils import send_fcm_notification
from myproject.observability import bound_log_context, capture_log_context
from users.models import Notification


try:
    _NOTIFICATION_SEND_WORKERS = max(1, int(os.getenv("NOTIFICATION_SEND_WORKERS", "4")))
except ValueError:
    _NOTIFICATION_SEND_WORKERS = 4
_NOTIFICATION_EXECUTOR = ThreadPoolExecutor(max_workers=_NOTIFICATION_SEND_WORKERS)
logger = structlog.get_logger(__name__)


def _send_notification_async(user, title, text, log_context=None):
    close_old_connections()
    try:
        with bound_log_context(
            **(log_context or {}),
            notification_user_id=user.id,
            notification_title=title,
        ):
            logger.info("notification_delivery_started")
            send_fcm_notification(user, title, text)
            logger.info("notification_delivery_finished")
    finally:
        close_old_connections()


def _enqueue_notification(user, title, text):
    log_context = capture_log_context()
    try:
        _NOTIFICATION_EXECUTOR.submit(_send_notification_async, user, title, text, log_context)
        logger.info(
            "notification_delivery_enqueued",
            notification_user_id=user.id,
            notification_title=title,
        )
    except Exception:
        # Fallback to sync if the executor is unavailable
        logger.exception(
            "notification_delivery_enqueue_failed",
            notification_user_id=user.id,
            notification_title=title,
        )
        send_fcm_notification(user, title, text)


def create_notification(user, title, text, source, duty_action=None):
    notification = Notification.objects.create(
        user=user, title=title, text=text, source=source, duty_action=duty_action
    )
    logger.info(
        "notification_created",
        notification_id=notification.id,
        notification_user_id=user.id,
        source=source,
        duty_action_id=duty_action.id if duty_action else None,
        title=title,
    )
    return notification


def create_and_notify(user, title, text, source, duty_action=None):
    notification = create_notification(
        user, title, text, source, duty_action=duty_action
    )
    _enqueue_notification(user, notification.title, notification.text)
    return notification


def notify_users(users, title, text, source, duty_action=None):
    logger.info(
        "notification_batch_started",
        source=source,
        duty_action_id=duty_action.id if duty_action else None,
        recipient_ids=sorted(user.id for user in users),
        recipient_count=len(users),
        title=title,
    )
    notifications = []
    for point_admin in users:
        notification = create_notification(
            point_admin, title, text, source, duty_action=duty_action
        )
        _enqueue_notification(point_admin, notification.title, notification.text)
        notifications.append(notification)
    return notifications


def notify_point_admins(point, title, text, source, duty_action=None):
    recipients = {u for u in chain(point.admins.all(), dispatch_admins())}
    logger.info(
        "notify_point_admins",
        point_id=point.id,
        title=title,
        recipient_ids=sorted(user.id for user in recipients),
        source=source,
        duty_action_id=duty_action.id if duty_action else None,
    )
    notify_users(
        recipients,
        title,
        text,
        source,
        duty_action=duty_action,
    )


def notify_duty_point_participants(point, title, text, source, duty_action=None):
    """
    Отправляет уведомление всем участникам системы дежурства (уровни 0–3 и ответственные лица).
    Уведомления сохраняются в истории (Notification).
    """
    if point is None:
        return []
    participants = get_duty_point_participants(point)
    logger.info(
        "notify_duty_point_participants",
        point_id=point.id,
        title=title,
        recipient_ids=sorted(participants.values_list("id", flat=True)),
        source=source,
        duty_action_id=duty_action.id if duty_action else None,
    )
    return notify_users(list(participants), title, text, source, duty_action=duty_action)


def notify_admins(title, text, source):
    notify_users(dispatch_admins(), title, text, source)
