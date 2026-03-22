# dispatch/cron.py
from datetime import timedelta, datetime, date

import structlog

from dispatch.models import DutyRole, DutyPoint, Duty
from dispatch.services.access import dispatch_admins
from dispatch.services.duties import (
    get_current_duties,
    get_duty_point_by_duty_role,
    get_duties_by_date,
)
from dispatch.services.notification import create_and_notify, notify_users, notify_point_admins
from dispatch.utils import now, today
from users.models import NotificationSourceEnum


logger = structlog.get_logger(__name__)


def need_to_open_notification():
    duties = get_current_duties(now(), start_offset=30)
    logger.info(
        "need_to_open_notification_started",
        run_at=datetime.now().isoformat(),
        duty_count=duties.count(),
    )

    coming_notifications = 0
    reminder_notifications = 0
    auto_opened_duties = 0
    for duty in duties:
        if not duty.is_opened and duty.notification_duty_is_coming is None:
            title = "Вам назначено дежурство сегодня"
            text = f"Дежурство в роли: {duty.role.name}"
            duty.notification_duty_is_coming = create_and_notify(duty.user, title, text, NotificationSourceEnum.DISPATCH.value)
            duty.save()
            coming_notifications += 1
            logger.info("duty_coming_notification_created", duty_id=duty.id, duty_user_id=duty.user_id)

        # Повторное уведомление через 15 минут после первого, если дежурство еще не принято
        if (not duty.is_opened 
                and duty.notification_duty_is_coming is not None 
                and duty.notification_duty_reminder is None
                and now() - duty.notification_duty_is_coming.created_at > timedelta(minutes=15)):
            title = "Напоминание: не забудьте принять дежурство"
            text = f"Дежурство в роли: {duty.role.name} еще не принято. Пожалуйста, откройте дежурство в приложении."
            duty.notification_duty_reminder = create_and_notify(duty.user, title, text, NotificationSourceEnum.DISPATCH.value)
            duty.save()
            reminder_notifications += 1
            logger.info("duty_reminder_notification_created", duty_id=duty.id, duty_user_id=duty.user_id)

        # Автоматическое открытие дежурства через 15 минут после напоминания, если дежурство еще не принято
        if (not duty.is_opened and duty.notification_need_to_open is None
                and duty.notification_duty_reminder is not None
                and now() - duty.notification_duty_reminder.created_at > timedelta(minutes=15)):
            duty.is_opened = True
            duty.is_forced_opened = True
            duty.notification_need_to_open = create_and_notify(
                duty.user,
                "Дежурство начато автоматически",
                f"Дежурство в роли: {duty.role.name}",
                NotificationSourceEnum.DISPATCH.value,
            )
            duty.save()
            auto_opened_duties += 1
            logger.warning(
                "duty_auto_opened_by_scheduler",
                duty_id=duty.id,
                duty_user_id=duty.user_id,
                duty_role_id=duty.role_id,
            )

            admins = list(dispatch_admins())
            for point in get_duty_point_by_duty_role(duty.role):
                admins.extend(point.admins.all())

            notify_users(
                admins,
                f"Пользователь {duty.user.display_name} не начал дежурство",
                f"Пользователь {duty.user.display_name} не начал дежурство в роли {duty.role.name}, оно было открыто автоматически.",
                NotificationSourceEnum.DISPATCH.value,
            )

    logger.info(
        "need_to_open_notification_finished",
        checked_duty_count=duties.count(),
        coming_notifications=coming_notifications,
        reminder_notifications=reminder_notifications,
        auto_opened_duties=auto_opened_duties,
    )


def check_missing_duties():
    """Проверяет отсутствующие дежурства на ближайшие 3 дня и уведомляет админов"""
    today_date = today()
    check_end_date = today_date + timedelta(days=3)
    logger.info(
        "check_missing_duties_started",
        run_at=datetime.now().isoformat(),
        start_date=today_date.isoformat(),
        end_date=check_end_date.isoformat(),
    )
    
    # Получаем все точки дежурств
    duty_points = DutyPoint.objects.all()
    points_with_missing_days = 0

    for point in duty_points:
        missing_days = []
        roles_to_check = []
        
        # Собираем все роли для этой точки
        if point.level_1_role:
            roles_to_check.append(point.level_1_role)
        if point.level_2_role:
            roles_to_check.append(point.level_2_role)
        if point.level_3_role:
            roles_to_check.append(point.level_3_role)
        
        # Проверяем каждую роль на наличие дежурств в ближайшие 3 дня
        for role in roles_to_check:
            current_date = today_date
            while current_date <= check_end_date:
                duties = get_duties_by_date(current_date, role)
                if not duties.exists():
                    missing_days.append((current_date, role))
                current_date += timedelta(days=1)
        
        # Если есть хотя бы один день без дежурства, уведомляем админов точки
        if missing_days:
            points_with_missing_days += 1
            missing_info = []
            for missing_date, missing_role in missing_days:
                missing_info.append(f"{missing_date.strftime('%d.%m.%Y')} - {missing_role.name}")
            
            title = f"Отсутствуют дежурства в системе {point.name}"
            text = f"В ближайшие 3 дня не назначены дежурства:\n" + "\n".join(missing_info)
            
            notify_point_admins(
                point,
                title,
                text,
                NotificationSourceEnum.DISPATCH.value,
            )

            logger.warning(
                "missing_duties_detected",
                point_id=point.id,
                missing_days=[
                    {"date": missing_date.isoformat(), "role_id": missing_role.id}
                    for missing_date, missing_role in missing_days
                ],
            )

    logger.info(
        "check_missing_duties_finished",
        checked_point_count=duty_points.count(),
        points_with_missing_days=points_with_missing_days,
    )

