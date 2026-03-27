from datetime import date, timedelta, datetime

from django.db.models import Q
from django.contrib.auth import get_user_model

from dispatch.models import Duty, DutyRole, ExploitationRole, DutyPoint
from dispatch.utils import now
from myproject.settings import AUTH_USER_MODEL


def get_all_duties():
    return Duty.objects.all()


def get_duty_by_id(duty_id):
    return Duty.objects.get(pk=duty_id)


def get_duties_by_date(start_date: date, role: DutyRole = None):
    if role is None:
        return Duty.objects.filter(start_datetime__date=start_date)
    return Duty.objects.filter(start_datetime__date=start_date, role=role)


def get_duties_covering_date(day_date: date, role: DutyRole = None):
    """
    Дежурства, которые покрывают данный день (включая многодневные).
    Для календарной проверки день считается покрытым, если он входит в диапазон
    [start_datetime.date(), end_datetime.date()).
    Это позволяет учитывать многодневные выходные дежурства, но не считать
    последний день покрытым целиком, если смена заканчивается утром.
    """
    qs = Duty.objects.filter(
        start_datetime__date__lte=day_date,
        end_datetime__date__gt=day_date,
    )
    if role is not None:
        qs = qs.filter(role=role)
    return qs


def get_duties_assigned(start_date: date, duty_role: DutyRole):
    """Число подряд идущих дней с сегодня, в которые есть дежурство (учитываются многодневные)."""
    counter = 0
    current_date = start_date
    while counter < 100:
        if not get_duties_covering_date(current_date, duty_role).exists():
            break
        counter += 1
        current_date += timedelta(days=1)
    return counter


def get_current_duties(current_datetime, user: AUTH_USER_MODEL = None, role: DutyRole = None, start_offset: int = 15):
    START_OFFSET = timedelta(minutes=start_offset)

    queryset = Duty.objects.filter(start_datetime__lte=current_datetime + START_OFFSET,
                                   end_datetime__gt=current_datetime)
    if user is not None:
        queryset = queryset.filter(user=user)
    if role is not None:
        queryset = queryset.filter(role=role)
    return queryset


def get_related_duty_points(user: AUTH_USER_MODEL = None):
    active_roles = [duty.role for duty in get_current_duties(now(), user)]
    return DutyPoint.objects.filter(
        Q(level_0_role__in=user.exploitation_roles.all()) |
        Q(level_1_role__in=active_roles) |
        Q(level_2_role__in=active_roles) |
        Q(level_3_role__in=active_roles)
    )


def get_duty_point_by_exploitation_role(exploitation_role: ExploitationRole):
    return DutyPoint.objects.filter(level_0_role=exploitation_role)


def get_duty_point_by_duty_role(duty_role: DutyRole):
    return DutyPoint.objects.filter(Q(level_1_role=duty_role) | Q(level_2_role=duty_role) | Q(level_3_role=duty_role)).all()


def get_or_create_duty(duty_date: date, role: DutyRole, defaults):
    defaults['start_datetime'] = datetime(duty_date.year, duty_date.month, duty_date.day, 17, 30, 0)

    next_day = duty_date + timedelta(days=1)
    defaults['end_datetime'] = datetime(next_day.year, next_day.month, next_day.day, 8, 30, 0)
    return Duty.objects.get_or_create(start_datetime__date=duty_date, role=role, defaults=defaults)


def duty_overlaps_range(role: DutyRole, range_start: date, range_end: date) -> bool:
    """
    Проверяет, есть ли у роли дежурство, покрывающее хотя бы один день периода
    [range_start, range_end] в той же календарной семантике [start_date, end_date).
    """
    return Duty.objects.filter(
        role=role,
        start_datetime__date__lte=range_end,
        end_datetime__date__gt=range_start,
    ).exists()


def get_or_create_duty_range(
    range_start: date, range_end: date, role: DutyRole, defaults: dict
) -> tuple[Duty, bool]:
    """
    Создаёт одно дежурство на весь период [range_start, range_end] включительно.
    start_datetime = день до первого дня 17:30, end_datetime = день после последнего 8:30.
    Если такое дежурство уже есть (по дате начала и роли), возвращает его без создания.
    """
    duty_start_date = range_start - timedelta(days=1)
    day_after_end = range_end + timedelta(days=1)
    defaults = dict(defaults)
    defaults["start_datetime"] = datetime(
        duty_start_date.year, duty_start_date.month, duty_start_date.day, 17, 30, 0
    )
    defaults["end_datetime"] = datetime(
        day_after_end.year, day_after_end.month, day_after_end.day, 8, 30, 0
    )
    return Duty.objects.get_or_create(
        start_datetime__date=duty_start_date, role=role, defaults=defaults
    )


def delete_duty(duty_date: date, role: DutyRole):
    Duty.objects.filter(start_datetime__date=duty_date, role=role).delete()


def get_duty_point_participants(point: DutyPoint):
    """
    Участники системы дежурства: персонал уровня 0, дежурные уровней 1–3 и ответственные лица (начальник тех. отдела).
    Все получают уведомления по инцидентам и могут контролировать процесс.
    """
    User = get_user_model()
    user_ids = set()

    if point.level_0_role:
        user_ids.update(point.level_0_role.members.values_list('id', flat=True))
    user_ids.update(point.admins.values_list('id', flat=True))

    roles = [point.level_1_role, point.level_2_role, point.level_3_role]
    roles = [r for r in roles if r is not None]
    if roles:
        duty_user_ids = Duty.objects.filter(role__in=roles).values_list('user_id', flat=True).distinct()
        user_ids.update(duty_user_ids)

    return User.objects.filter(id__in=user_ids).distinct()
