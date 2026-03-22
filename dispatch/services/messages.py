from django.contrib.contenttypes.models import ContentType
import structlog

from dispatch.models import IncidentMessage, Incident, Duty, TextMessage
from myproject.settings import AUTH_USER_MODEL


logger = structlog.get_logger(__name__)


def create_system_message(incident, text):
    incident_message = IncidentMessage.objects.create(
        incident=incident,
        message_type=IncidentMessage.TEXT
    )

    text_message = TextMessage.objects.create(
        message=incident_message,
        text=text,
    )

    incident_message.content_type = ContentType.objects.get_for_model(TextMessage)
    incident_message.object_id = text_message.id
    incident_message.save()
    logger.info(
        "incident_system_message_created",
        incident_id=incident.id,
        incident_message_id=incident_message.id,
        text_message_id=text_message.id,
        message_text=text,
    )
    return incident_message


def create_escalation_error_message_duty_not_opened(incident, failed_level, not_opened_duty: Duty):
    logger.warning(
        "incident_escalation_error_message_created",
        incident_id=incident.id,
        failed_level=failed_level,
        duty_id=not_opened_duty.id,
        duty_user_id=not_opened_duty.user_id,
        duty_role_id=not_opened_duty.role_id,
    )
    return create_system_message(incident, f"Инцидент не удалось поднять до уровня {failed_level}, так как"
                                           f" ответственный дежурный {not_opened_duty.user} ({not_opened_duty.role})"
                                           f" не начал свое дежурство.")


def create_escalation_message(incident: Incident, to_level: int, escalation_author: AUTH_USER_MODEL, new_responsible_duty: Duty = None):
    message = f"Инцидент был поднят до уровня {to_level} пользователем {escalation_author.display_name}."
    if new_responsible_duty is not None:
        message += f" Новый ответственный дежурный: {new_responsible_duty.user} ({new_responsible_duty.role})."
    return create_system_message(incident, message)


def create_close_escalation_message(incident: Incident, user: AUTH_USER_MODEL):
    message = f"Инцидент был закрыт пользователем {user.display_name}."
    return create_system_message(incident, message)


def create_force_close_escalation_message(incident: Incident, user: AUTH_USER_MODEL):
    message = f"Инцидент был принудительно закрыт пользователем {user.display_name}."
    return create_system_message(incident, message)


def create_reopen_escalation_message(incident: Incident, user: AUTH_USER_MODEL):
    message = f"Инцидент был переоткрыт пользователем {user.display_name}."
    return create_system_message(incident, message)


def create_incident_acceptance_message(incident: Incident, user: AUTH_USER_MODEL):
    message = f"Инцидент был принят пользователем {user.display_name}."
    return create_system_message(incident, message)
