import enum
import os
import uuid

from django.core.exceptions import ValidationError
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import UniqueConstraint
from django.db.models.functions import TruncDate
from django.utils.deconstruct import deconstructible
from storages.backends.s3boto3 import S3Boto3Storage

from myproject import settings
from myproject.settings import AUTH_USER_MODEL
from dispatch.utils import now


class DispatchS3MediaStorage(S3Boto3Storage):
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    location = 'dispatch_media'
    file_overwrite = False


class DutyRole(models.Model):
    name = models.CharField(max_length=100, verbose_name='Имя роли')

    class Meta:
        verbose_name = "Роль диспетчеризации"
        verbose_name_plural = "Роли диспетчеризации"

    def __str__(self):
        return f"{self.name}"


class ExploitationRole(models.Model):
    name = models.CharField(max_length=100, verbose_name='Имя роли')

    members = models.ManyToManyField(AUTH_USER_MODEL, related_name='exploitation_roles', verbose_name='Участники',
                                     blank=True)

    class Meta:
        verbose_name = "Роль эксплуатации"
        verbose_name_plural = "Роли эксплуатации"

    def __str__(self):
        return f"{self.name}"


class DutyPoint(models.Model):
    name = models.CharField(max_length=150, verbose_name='Имя системы дежурств')
    level_0_role = models.ForeignKey(ExploitationRole, on_delete=models.SET_NULL, null=True, blank=True,
                                     verbose_name='Персонал, вызывающий дежурного (уровень 0)', related_name='level_0_role')
    level_1_role = models.ForeignKey(DutyRole, on_delete=models.SET_NULL, null=True, blank=True,
                                     verbose_name='Дежурный уровня 1', related_name='level_1_role')
    level_2_role = models.ForeignKey(DutyRole, on_delete=models.SET_NULL, null=True, blank=True,
                                     verbose_name='Дежурный уровня 2', related_name='level_2_role')
    level_3_role = models.ForeignKey(DutyRole, on_delete=models.SET_NULL, null=True, blank=True,
                                     verbose_name='Дежурный уровня 3', related_name='level_3_role')

    admins = models.ManyToManyField(AUTH_USER_MODEL, related_name='admin_duty_points', verbose_name='Ответственные лица')

    class Meta:
        verbose_name = "Система дежурства"
        verbose_name_plural = "Системы дежурств"

    def __str__(self):
        return f"{self.name}"


class Duty(models.Model):
    user = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name='Аккаунт дежурного')
    role = models.ForeignKey(DutyRole, on_delete=models.CASCADE, null=True, verbose_name='Роль дежурства')
    is_opened = models.BooleanField(default=False, verbose_name='Открыт ли')
    is_forced_opened = models.BooleanField(default=False, verbose_name='Открыт ли автоматически')

    start_datetime = models.DateTimeField(verbose_name='Начало дежурства', null=False, blank=False)
    end_datetime = models.DateTimeField(verbose_name='Окончание дежурства', null=False, blank=False)

    # Нотификация о том, что у человека сегодня будет дежурство
    notification_duty_is_coming = models.ForeignKey("users.Notification", on_delete=models.SET_NULL, null=True, blank=True, related_name="duty_is_coming")

    # Повторное уведомление о дежурстве через 15 минут после первого
    notification_duty_reminder = models.ForeignKey("users.Notification", on_delete=models.SET_NULL, null=True, blank=True, related_name="duty_reminder")

    # Нотификация о том, что ответственный человек не принял дежурство
    notification_need_to_open = models.ForeignKey("users.Notification", on_delete=models.SET_NULL, null=True, blank=True, related_name="need_to_open")

    class Meta:
        verbose_name = "Дежурство"
        verbose_name_plural = "Дежурства"

        constraints = [
            UniqueConstraint(
                TruncDate('start_datetime'),
                'role',
                name='unique_start_date',
            ),
        ]

    @property
    def date(self):
        return self.start_datetime.date() if self.start_datetime else None

    def has_ended(self, current_datetime=None):
        current_datetime = current_datetime or now()
        return bool(self.end_datetime and self.end_datetime <= current_datetime)

    def clean(self):
        if not self.pk:
            return

        original = type(self).objects.filter(pk=self.pk).only('end_datetime').first()
        if original and original.end_datetime and original.end_datetime <= now():
            raise ValidationError("Нельзя редактировать завершенное дежурство.")

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} - {self.date} ({self.role})"


class WeekendDutyAssignment(models.Model):
    role = models.ForeignKey(
        DutyRole,
        on_delete=models.CASCADE,
        related_name="weekend_assignments",
        verbose_name="Роль дежурства",
    )
    user = models.ForeignKey(
        AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        verbose_name="Дежурный",
    )
    is_active = models.BooleanField(default=True, verbose_name="Активно")

    class Meta:
        verbose_name = "Дежурный на выходные"
        verbose_name_plural = "Дежурные на выходные"
        constraints = [
            UniqueConstraint(fields=["role"], name="unique_weekend_assignment"),
        ]

    def __str__(self):
        return f"{self.role}: {self.user}"


class DutyActionTypeEnum(enum.Enum):
    REFUSAL = "refusal"  # Отказ от дежурства
    TRANSFER = "transfer"  # Передача дежурства
    ACCEPTANCE = "acceptance"  # Принятие дежурства


class DutyAction(models.Model):
    """Модель для хранения действий с дежурствами (отказы, передачи, принятия)"""

    ACTION_CHOICES = [
        (DutyActionTypeEnum.REFUSAL.value, "Отказ от дежурства"),
        (DutyActionTypeEnum.TRANSFER.value, "Передача дежурства"),
        (DutyActionTypeEnum.ACCEPTANCE.value, "Принятие дежурства"),
    ]

    duty = models.ForeignKey(
        Duty, on_delete=models.CASCADE, related_name="actions", verbose_name="Дежурство"
    )
    user = models.ForeignKey(
        AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name="Пользователь"
    )
    action_type = models.CharField(
        max_length=20, choices=ACTION_CHOICES, verbose_name="Тип действия"
    )
    reason = models.TextField(verbose_name="Причина", blank=True, null=True)
    new_user = models.ForeignKey(
        AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_duty_actions",
        verbose_name="Новый дежурный",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Время создания")
    is_resolved = models.BooleanField(default=False, verbose_name="Решено ли")
    resolved_by = models.ForeignKey(
        AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_duty_actions",
        verbose_name="Решено пользователем",
    )
    resolved_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Время решения"
    )

    class Meta:
        verbose_name = "Действие с дежурством"
        verbose_name_plural = "Действия с дежурствами"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_type_display()} для {self.duty} от {self.user}"


class IncidentStatusEnum(enum.Enum):
    OPENED = 'opened'
    CLOSED = 'closed'
    FORCE_CLOSED = 'force_closed'
    WAITING_TO_BE_ACCEPTED = 'waiting_to_be_accepted'


class Incident(models.Model):
    STATUS_CHOICES = [
        (IncidentStatusEnum.OPENED.value, 'В работе'),
        (IncidentStatusEnum.CLOSED.value, 'Выполнено'),
        (IncidentStatusEnum.FORCE_CLOSED.value, 'Ненадлежащее выполнение'),
        (IncidentStatusEnum.WAITING_TO_BE_ACCEPTED.value, 'В ожидании принятия'),
    ]

    name = models.CharField(max_length=255, verbose_name='Имя инцидента')
    description = models.TextField(verbose_name='Описание инцидента')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='opened', verbose_name='Статус')
    level = models.PositiveSmallIntegerField(default=0, verbose_name='Уровень')
    is_critical = models.BooleanField(default=False, verbose_name='Критичный',
                                      help_text='Автоматически выставляется, если ни один из уровней не справился с выполнением')
    author = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                               related_name='opened_incident',
                               verbose_name='Автор')
    responsible_user = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='responsible_incidents', verbose_name='Ответственный дежурный')
    point = models.ForeignKey(DutyPoint, on_delete=models.SET_NULL, null=True, related_name='incidents',
                              verbose_name='Система дежурства')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Время создания')

    # is_accepted = models.BooleanField(default=False, verbose_name='Необходимо открыть дежурство ответсвенному в приложении')

    class Meta:
        verbose_name = "Инцидент"
        verbose_name_plural = "Инциденты"

    def __str__(self):
        return f'{self.name} ({self.get_status_display()})'


class IncidentMessage(models.Model):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"

    MESSAGE_TYPES = [
        (TEXT, "Текст"),
        (PHOTO, "Фото"),
        (VIDEO, "Видео"),
        (AUDIO, "Аудио"),
    ]

    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name='messages')

    user = models.ForeignKey(AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                             verbose_name="Отправитель")
    created_at = models.DateTimeField(auto_now_add=True)
    message_type = models.CharField(max_length=10, choices=MESSAGE_TYPES)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Message from {self.user or 'system'} ({self.content_type}) at {self.created_at}"


@deconstructible
class PathAndRename(object):

    def __init__(self, sub_path):
        self.path = sub_path

    def __call__(self, instance, filename):
        ext = filename.split('.')[-1]
        filename = '{}.{}'.format(uuid.uuid4().hex, ext)
        return os.path.join(self.path, filename)


class TextMessage(models.Model):
    message = models.OneToOneField(IncidentMessage, on_delete=models.CASCADE, related_name="text")
    text = models.TextField(verbose_name="Текст")

    def __str__(self):
        return f"{self.text}"


class PhotoMessage(models.Model):
    message = models.OneToOneField(IncidentMessage, on_delete=models.CASCADE, related_name="photo")
    photo = models.ImageField(storage=DispatchS3MediaStorage(), upload_to=PathAndRename("photos"))


class VideoMessage(models.Model):
    message = models.OneToOneField(IncidentMessage, on_delete=models.CASCADE, related_name="video")
    video = models.FileField(storage=DispatchS3MediaStorage(), upload_to=PathAndRename("videos"))


class AudioMessage(models.Model):
    message = models.OneToOneField(IncidentMessage, on_delete=models.CASCADE, related_name="audio")
    audio = models.FileField(storage=DispatchS3MediaStorage(), upload_to=PathAndRename("audios"))
