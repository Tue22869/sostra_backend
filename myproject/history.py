from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from simple_history import register

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
from food.models import AllowedDish, Dish, Feedback, Order
from myapp.models import Device, Guard, Message, Point, Round, Visit
from users.models import Notification, PasswordResetToken


_HISTORY_REGISTERED = False


def register_model_histories() -> None:
    global _HISTORY_REGISTERED
    if _HISTORY_REGISTERED:
        return

    model_configs = [
        (get_user_model(), {"m2m_fields": ["groups"]}),
        (Group, {"app": "users", "m2m_fields": ["permissions"]}),
        (Guard, {"m2m_fields": ["managers"]}),
        (Point, {}),
        (Round, {}),
        (Visit, {}),
        (Message, {}),
        (Device, {}),
        (Dish, {}),
        (AllowedDish, {}),
        (Order, {}),
        (Feedback, {}),
        (DutyRole, {}),
        (ExploitationRole, {"m2m_fields": ["members"]}),
        (DutyPoint, {"m2m_fields": ["admins"]}),
        (Duty, {}),
        (WeekendDutyAssignment, {}),
        (DutyAction, {}),
        (Incident, {}),
        (IncidentMessage, {}),
        (TextMessage, {}),
        (PhotoMessage, {}),
        (VideoMessage, {}),
        (AudioMessage, {}),
        (Notification, {}),
        (PasswordResetToken, {}),
    ]

    for model, config in model_configs:
        if hasattr(model, "history"):
            continue
        register(model, **config)

    _HISTORY_REGISTERED = True
