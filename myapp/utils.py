import os

import structlog
from django.core.management import call_command
from pyfcm import FCMNotification

from myapp.models import Device
from myproject.settings import AUTH_USER_MODEL


logger = structlog.get_logger(__name__)


def telegram_notification(tg_user_id, message):
    """
    Отправить уведомление пользователю через вашу management-команду.
    """
    logger.info("telegram_notification_send_started", telegram_user_id=tg_user_id)
    call_command('sendnotification', str(tg_user_id), message)
    logger.info("telegram_notification_send_finished", telegram_user_id=tg_user_id)


def send_fcm_notification(user: AUTH_USER_MODEL, title, body, data=None):
    try:
        fcm = FCMNotification(service_account_file=os.getenv('PATH_TO_GOOGLE_OAUTH_TOKEN'),
                            project_id=os.getenv('FIREBASE_PROJECT_ID'))

        if Device.objects.filter(user=user).exists() and user.device.notification_token is not None:
            logger.info(
                "fcm_notification_send_started",
                user_id=user.id,
                notification_title=title,
            )
            result = fcm.notify(
                fcm_token=user.device.notification_token,
                notification_title=title,
                notification_body=body,
                webpush_config={
                    "fcm_options": {
                        "link": "https://web.appsostra.ru/#/notifications"
                    },
                    "notification": {
                        "title": title,
                        "body": body,
                        # "icon": "https://appsostra.ru/icons/icon-192.png",
                        # "badge": "https://appsostra.ru/icons/badge.png",
                    },
                },
            )
            logger.info(
                "fcm_notification_send_finished",
                user_id=user.id,
                notification_title=title,
                provider_response=result,
            )
            return result
    except Exception as e:
        logger.exception(
            "fcm_notification_send_failed",
            user_id=user.id,
            notification_title=title,
        )

    if user.telegram_user_id is not None:
        logger.info(
            "notification_fallback_to_telegram",
            user_id=user.id,
            telegram_user_id=user.telegram_user_id,
            notification_title=title,
        )
        telegram_notification(user.telegram_user_id, title + '\n\n' + body)
