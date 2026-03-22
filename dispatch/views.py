from datetime import time

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.utils.dateparse import parse_date
import structlog
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response

from users.models import NotificationSourceEnum

from .calendar_ru import is_working_day
from .models import (
    DutyAction,
    DutyActionTypeEnum,
    Incident,
    IncidentMessage,
    IncidentStatusEnum,
)
from .serializers import (
    AudioMessageSerializer,
    DutyPointSerializer,
    DutySerializer,
    IncidentMessageSerializer,
    IncidentSerializer,
    PhotoMessageSerializer,
    TextMessageSerializer,
    VideoMessageSerializer,
)
from .services.access import has_dispatch_admin_rights
from .services.duties import (
    get_all_duties,
    get_current_duties,
    get_duties_by_date,
    get_duty_by_id,
    get_duty_point_by_duty_role,
    get_related_duty_points,
)
from .services.incidents import escalate_incident, user_incidents
from .services.incident_statistics import get_incident_statistics
from .services.messages import (
    create_close_escalation_message,
    create_force_close_escalation_message,
    create_incident_acceptance_message,
    create_reopen_escalation_message,
)
from .services.notification import create_and_notify, notify_point_admins, notify_duty_point_participants
from .utils import now


logger = structlog.get_logger(__name__)


def _incident_context(incident):
    return {
        "incident_id": incident.id,
        "incident_status": incident.status,
        "incident_level": incident.level,
        "incident_point_id": incident.point_id,
        "incident_responsible_user_id": incident.responsible_user_id,
    }


def _duty_context(duty):
    return {
        "duty_id": duty.id,
        "duty_user_id": duty.user_id,
        "duty_role_id": duty.role_id,
        "duty_is_opened": duty.is_opened,
        "duty_is_forced_opened": duty.is_forced_opened,
    }


class ListRetrieveOnlyPermission(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True

        if hasattr(view, 'action'):
            return view.action in ['list', 'retrieve']

        return False


class DutyPointViewSet(viewsets.ModelViewSet):
    serializer_class = DutyPointSerializer
    permission_classes = [ListRetrieveOnlyPermission, IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return get_related_duty_points(user)


class IncidentViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        incidents = Incident.objects.all()
        serializer = IncidentSerializer(incidents, many=True)
        return Response(serializer.data)

    def retrieve(self, request, pk=None):
        incident = Incident.objects.get(pk=pk)
        serializer = IncidentSerializer(incident)
        return Response(serializer.data)

    def create(self, request):
        current_dt = now()
        current_time = current_dt.time().replace(tzinfo=None)
        if is_working_day(current_dt.date()) and (time(8, 30) <= current_time <= time(17, 30)):
            logger.warning(
                "incident_create_blocked_working_hours",
                requested_by_user_id=request.user.id,
                request_point_id=request.data.get("point_id"),
                request_incident_name=request.data.get("name"),
            )
            return Response(
                {"error": "Создание инцидентов запрещено в рабочие дни с 08:30 до 17:30."},
                status=403,
            )

        logger.info(
            "incident_create_requested",
            requested_by_user_id=request.user.id,
            request_point_id=request.data.get("point_id"),
            request_incident_name=request.data.get("name"),
            is_critical=request.data.get("is_critical"),
        )
        serializer = IncidentSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            incident = serializer.save()
            logger.info(
                "incident_created_via_api",
                **_incident_context(incident),
                author_id=incident.author_id,
                incident_name=incident.name,
            )
            if incident.point:
                notify_duty_point_participants(
                    incident.point,
                    incident.name,
                    f"Создан инцидент: {incident.name}. {incident.description[:100]}{'…' if len(incident.description) > 100 else ''}",
                    NotificationSourceEnum.DISPATCH.value,
                )
            escalate_incident(incident, request.user)
            return Response(serializer.data, status=201)
        logger.warning(
            "incident_create_validation_failed",
            requested_by_user_id=request.user.id,
            errors=serializer.errors,
        )
        return Response(serializer.errors, status=400)

    @action(detail=True, methods=["get"])
    def available_actions(self, request, pk=None):
        incident = Incident.objects.get(pk=pk)

        is_admin = has_dispatch_admin_rights(request.user, incident.point)

        response = []

        if (incident.responsible_user == request.user and incident.status != IncidentStatusEnum.WAITING_TO_BE_ACCEPTED) or is_admin:
            if incident.status != IncidentStatusEnum.OPENED.value:
                response.append(IncidentStatusEnum.OPENED.value)
            if incident.status != IncidentStatusEnum.CLOSED.value:
                response.append(IncidentStatusEnum.CLOSED.value)

            if incident.level < 4:
                response.append("escalate")

            if is_admin:
                if incident.status != IncidentStatusEnum.FORCE_CLOSED.value:
                    response.append(IncidentStatusEnum.FORCE_CLOSED.value)

        return Response(response)

    @action(detail=True, methods=["post"])
    def change_status(self, request, pk=None):
        incident = Incident.objects.get(pk=pk)

        is_admin = has_dispatch_admin_rights(request.user, incident.point)

        if incident.responsible_user != request.user and not is_admin:
            logger.warning(
                "incident_status_change_forbidden",
                **_incident_context(incident),
                requested_by_user_id=request.user.id,
                requested_status=request.data.get("status"),
            )
            return Response({"error": "Вы не являетесь ответственным за этот инцидент"}, status=403)

        new_status = request.data.get("status")
        logger.info(
            "incident_status_change_requested",
            **_incident_context(incident),
            requested_by_user_id=request.user.id,
            requested_status=new_status,
            is_dispatch_admin=is_admin,
        )

        if new_status not in [e.value for e in IncidentStatusEnum]:
            logger.warning(
                "incident_status_change_invalid_status",
                **_incident_context(incident),
                requested_by_user_id=request.user.id,
                requested_status=new_status,
            )
            return Response({"error": "Некорректный статус"}, status=400)

        if new_status in ["force_closed"] and not is_admin:
            logger.warning(
                "incident_force_close_forbidden",
                **_incident_context(incident),
                requested_by_user_id=request.user.id,
            )
            return Response({"error": "Принудительно закрыть может только dispatch_admin_manager"}, status=400)

        old_status = incident.status
        incident.status = new_status
        incident.save()

        if new_status == "closed":
            create_close_escalation_message(incident, request.user)
            if incident.point:
                notify_duty_point_participants(
                    incident.point,
                    incident.name,
                    f"Инцидент закрыт пользователем {request.user.display_name}.",
                    NotificationSourceEnum.DISPATCH.value,
                )
        elif new_status == "force_closed":
            create_force_close_escalation_message(incident, request.user)
            if incident.point:
                notify_duty_point_participants(
                    incident.point,
                    incident.name,
                    f"Инцидент принудительно закрыт пользователем {request.user.display_name}.",
                    NotificationSourceEnum.DISPATCH.value,
                )
        elif new_status == "opened":
            if old_status in [IncidentStatusEnum.FORCE_CLOSED.value, IncidentStatusEnum.CLOSED.value]:
                create_reopen_escalation_message(incident, request.user)
                if incident.point:
                    notify_duty_point_participants(
                        incident.point,
                        incident.name,
                        f"Инцидент переоткрыт пользователем {request.user.display_name}.",
                        NotificationSourceEnum.DISPATCH.value,
                    )
            elif old_status == IncidentStatusEnum.WAITING_TO_BE_ACCEPTED.value:
                if incident.responsible_user == request.user:
                    create_incident_acceptance_message(incident, request.user)
                    if incident.point:
                        notify_duty_point_participants(
                            incident.point,
                            incident.name,
                            f"Инцидент принят пользователем {request.user.display_name}.",
                            NotificationSourceEnum.DISPATCH.value,
                        )
                else:
                    logger.warning(
                        "incident_acceptance_forbidden",
                        **_incident_context(incident),
                        requested_by_user_id=request.user.id,
                    )
                    return Response({"error": "Принять инцидент может только ответственный"}, status=403)

        logger.info(
            "incident_status_change_finished",
            **_incident_context(incident),
            requested_by_user_id=request.user.id,
            old_status=old_status,
            new_status=new_status,
        )
        serializer = IncidentSerializer(incident)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def escalate(self, request, pk=None):
        incident = Incident.objects.get(pk=pk)

        is_admin = has_dispatch_admin_rights(request.user, incident.point)

        if incident.responsible_user != request.user and not is_admin:
            logger.warning(
                "incident_escalation_forbidden",
                **_incident_context(incident),
                requested_by_user_id=request.user.id,
            )
            return Response({"error": "Вы не являетесь ответственным за этот инцидент"}, status=403)

        logger.info(
            "incident_escalation_requested",
            **_incident_context(incident),
            requested_by_user_id=request.user.id,
            is_dispatch_admin=is_admin,
        )
        escalate_incident(incident, request.user)

        logger.info(
            "incident_escalation_completed",
            **_incident_context(incident),
            requested_by_user_id=request.user.id,
        )
        serializer = IncidentSerializer(incident)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def my_incidents(self, request):
        incidents = user_incidents(request.user)

        serializer = IncidentSerializer(incidents, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def statistics(self, request):
        """
        Получает статистику по инцидентам с фильтрами
        
        Параметры запроса:
        - start_date: Дата начала периода (YYYY-MM-DD)
        - end_date: Дата окончания периода (YYYY-MM-DD)
        - status: Статус инцидента (opened, closed, force_closed, waiting_to_be_accepted)
        - responsible_user_id: ID ответственного дежурного
        - point_id: ID системы дежурства
        - author_id: ID автора инцидента
        """
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        status = request.query_params.get('status')
        responsible_user_id = request.query_params.get('responsible_user_id')
        point_id = request.query_params.get('point_id')
        author_id = request.query_params.get('author_id')
        
        start_date = None
        end_date = None
        
        if start_date_str:
            try:
                start_date = parse_date(start_date_str)
            except (ValueError, TypeError):
                return Response({"error": "Неверный формат start_date. Используйте YYYY-MM-DD."}, status=400)
        
        if end_date_str:
            try:
                end_date = parse_date(end_date_str)
            except (ValueError, TypeError):
                return Response({"error": "Неверный формат end_date. Используйте YYYY-MM-DD."}, status=400)
        
        responsible_user_id_int = None
        if responsible_user_id:
            try:
                responsible_user_id_int = int(responsible_user_id)
            except (ValueError, TypeError):
                return Response({"error": "responsible_user_id должен быть числом"}, status=400)
        
        point_id_int = None
        if point_id:
            try:
                point_id_int = int(point_id)
            except (ValueError, TypeError):
                return Response({"error": "point_id должен быть числом"}, status=400)
        
        author_id_int = None
        if author_id:
            try:
                author_id_int = int(author_id)
            except (ValueError, TypeError):
                return Response({"error": "author_id должен быть числом"}, status=400)
        
        statistics = get_incident_statistics(
            start_date=start_date,
            end_date=end_date,
            status=status,
            responsible_user_id=responsible_user_id_int,
            point_id=point_id_int,
            author_id=author_id_int,
        )
        
        return Response(statistics)


class DutyViewSet(viewsets.ReadOnlyModelViewSet):  # ReadOnly since no update/create
    permission_classes = [IsAuthenticated]
    serializer_class = DutySerializer
    queryset = get_all_duties()
    filterset_fields = ['date', 'role', 'is_opened']

    def get_queryset(self):
        queryset = super().get_queryset()
        date_str = self.request.query_params.get('date')

        if date_str:
            date = parse_date(date_str)
            if date:
                queryset = get_duties_by_date(date)

        return queryset

    @action(detail=False, methods=['get'])
    def my_duties(self, request):
        serializer = DutySerializer(get_current_duties(now(), request.user, start_offset=30), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def open(self, request, pk=None):
        duty = get_duty_by_id(pk)

        if duty.has_ended():
            logger.warning(
                "duty_open_rejected_ended",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
            )
            return Response({"error": "Нельзя открыть завершённое дежурство"}, status=400)

        if duty.user != request.user:
            logger.warning(
                "duty_open_forbidden",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
            )
            return Response({"error": "Открыть дежурство может только сам дежурный"}, status=403)

        logger.info("duty_open_requested", **_duty_context(duty), requested_by_user_id=request.user.id)
        duty.is_opened = True
        duty.save()

        logger.info("duty_open_finished", **_duty_context(duty), requested_by_user_id=request.user.id)
        serializer = DutySerializer(duty)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def transfer_duty(self, request, pk=None):
        duty = get_duty_by_id(pk)

        if duty.has_ended():
            logger.warning(
                "duty_transfer_rejected_ended",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
            )
            return Response({"error": "Нельзя изменять завершённое дежурство"}, status=400)

        if duty.user != request.user:
            logger.warning(
                "duty_transfer_forbidden",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
            )
            return Response({"error": "Передать дежурство может только сам дежурный"}, status=403)

        new_user_id = request.data.get("user_id")
        user_reason = request.data.get("user_reason")
        if not user_reason or len(user_reason) == 0:
            user_reason = "не указана"

        logger.info(
            "duty_transfer_requested",
            **_duty_context(duty),
            requested_by_user_id=request.user.id,
            requested_new_user_id=new_user_id,
            reason=user_reason,
        )

        if new_user_id == 0:
            # Создаем запись об отказе
            duty_action = DutyAction.objects.create(
                duty=duty,
                user=request.user,
                action_type=DutyActionTypeEnum.REFUSAL.value,
                reason=user_reason,
                is_resolved=False,
            )
            logger.warning(
                "duty_refusal_created",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
                duty_action_id=duty_action.id,
                reason=user_reason,
            )

            for point in get_duty_point_by_duty_role(duty.role):
                import threading

                threading.Thread(
                    target=notify_point_admins,
                    args=(
                        point,
                        f"{duty.user.display_name} не может выйти на дежурство",
                        f"Необходимо найти замену для {duty.role.name} на системе дежурств {point.name}. Причина пользователя: {user_reason}",
                        NotificationSourceEnum.DISPATCH.value,
                    ),
                    kwargs={'duty_action': duty_action},
                    daemon=True
                ).start()
            return Response(status=204)

        if not get_user_model().objects.filter(pk=new_user_id).exists():
            logger.warning(
                "duty_transfer_invalid_new_user",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
                requested_new_user_id=new_user_id,
            )
            return Response(
                {"error": "Поле user_id в теле запроса некорректное"}, status=403
            )

        new_user = get_user_model().objects.get(pk=new_user_id)

        # Создаем запись о передаче дежурства
        duty_action = DutyAction.objects.create(
            duty=duty,
            user=request.user,
            action_type=DutyActionTypeEnum.TRANSFER.value,
            reason=user_reason,
            new_user=new_user,
            is_resolved=False,
        )

        duty.user = new_user
        duty.notification_duty_is_coming = None
        duty.notification_need_to_open = None
        duty.save()

        serializer = DutySerializer(duty)
        logger.info(
            "duty_transfer_finished",
            **_duty_context(duty),
            requested_by_user_id=request.user.id,
            previous_user_id=request.user.id,
            new_user_id=new_user.id,
            duty_action_id=duty_action.id,
            reason=user_reason,
        )
        create_and_notify(
            new_user,
            "Вам передано дежурство",
            f"{request.user.display_name} передал вам дежурство",
            NotificationSourceEnum.DISPATCH.value,
            duty_action=duty_action,
        )

        for point in get_duty_point_by_duty_role(duty.role):
            notify_point_admins(
                point,
                f"{request.user.display_name} не может выйти на дежурство",
                f"{request.user.display_name} передал {new_user.display_name} дежурство {duty.role.name} на системе дежурств {point.name}. Причина пользователя: {user_reason}",
                NotificationSourceEnum.DISPATCH.value,
                duty_action=duty_action,
            )

        return Response(serializer.data)

    @action(detail=False, methods=["post"])
    def reassign_by_notification(self, request):
        """Переназначить дежурство админом через уведомление"""
        notification_id = request.data.get("notification_id")
        if not notification_id:
            logger.warning(
                "duty_reassign_missing_notification_id",
                requested_by_user_id=request.user.id,
            )
            return Response({"error": "Необходимо указать notification_id"}, status=400)

        try:
            from users.models import Notification

            notification = Notification.objects.get(
                id=notification_id, user=request.user
            )
        except Notification.DoesNotExist:
            logger.warning(
                "duty_reassign_notification_not_found",
                requested_by_user_id=request.user.id,
                notification_id=notification_id,
            )
            return Response({"error": "Уведомление не найдено"}, status=404)

        if not notification.duty_action:
            logger.warning(
                "duty_reassign_notification_without_action",
                requested_by_user_id=request.user.id,
                notification_id=notification.id,
            )
            return Response(
                {"error": "Уведомление не связано с действием дежурства"}, status=400
            )

        duty_action = notification.duty_action
        duty = duty_action.duty

        if duty.has_ended():
            logger.warning(
                "duty_reassign_rejected_ended",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
                notification_id=notification.id,
            )
            return Response({"error": "Нельзя изменять завершённое дежурство"}, status=400)

        # Проверяем права админа
        from .models import DutyPoint
        from .services.access import dispatch_admins

        is_dispatch_admin = request.user in dispatch_admins()
        is_point_admin = (
            DutyPoint.objects.filter(
                admins=request.user, level_1_role=duty.role
            ).exists()
            or DutyPoint.objects.filter(
                admins=request.user, level_2_role=duty.role
            ).exists()
            or DutyPoint.objects.filter(
                admins=request.user, level_3_role=duty.role
            ).exists()
        )

        if not (is_dispatch_admin or is_point_admin):
            logger.warning(
                "duty_reassign_forbidden",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
                notification_id=notification.id,
            )
            return Response(
                {"error": "Только админ может переназначить дежурного"}, status=403
            )

        new_user_id = request.data.get("user_id")
        if not new_user_id:
            logger.warning(
                "duty_reassign_missing_new_user",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
                notification_id=notification.id,
            )
            return Response({"error": "Необходимо указать user_id"}, status=400)

        if not get_user_model().objects.filter(pk=new_user_id).exists():
            logger.warning(
                "duty_reassign_new_user_not_found",
                **_duty_context(duty),
                requested_by_user_id=request.user.id,
                requested_new_user_id=new_user_id,
            )
            return Response({"error": "Пользователь не найден"}, status=404)

        new_user = get_user_model().objects.get(pk=new_user_id)
        old_user = duty.user
        logger.info(
            "duty_reassign_requested",
            **_duty_context(duty),
            requested_by_user_id=request.user.id,
            old_user_id=old_user.id,
            new_user_id=new_user.id,
            notification_id=notification.id,
            duty_action_id=duty_action.id,
        )

        # Переназначаем дежурство
        duty.user = new_user
        duty.notification_duty_is_coming = None
        duty.notification_need_to_open = None
        duty.save()

        # Помечаем связанные нерешенные действия как решенные
        unresolved_actions = DutyAction.objects.filter(duty=duty, is_resolved=False)
        for duty_action_item in unresolved_actions:
            duty_action_item.is_resolved = True
            duty_action_item.resolved_by = request.user
            duty_action_item.resolved_at = timezone.now()
            duty_action_item.save()

        # Уведомляем нового дежурного
        create_and_notify(
            new_user,
            "Вам назначено дежурство",
            f"Администратор {request.user.display_name} назначил вам дежурство {duty.role.name} на {duty.date}",
            NotificationSourceEnum.DISPATCH.value,
        )

        # Уведомляем старого дежурного
        if old_user != new_user:
            create_and_notify(
                old_user,
                "Дежурство переназначено",
                f"Администратор {request.user.display_name} переназначил ваше дежурство {duty.role.name} на {duty.date} пользователю {new_user.display_name}",
                NotificationSourceEnum.DISPATCH.value,
            )

        logger.info(
            "duty_reassign_finished",
            **_duty_context(duty),
            requested_by_user_id=request.user.id,
            old_user_id=old_user.id,
            new_user_id=new_user.id,
            notification_id=notification.id,
            resolved_action_count=unresolved_actions.count(),
        )
        serializer = DutySerializer(duty)
        return Response(serializer.data)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 5
    page_size_query_param = 'page_size'
    max_page_size = 1000


class IncidentMessageViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)
    serializer_class = IncidentMessageSerializer

    def get_queryset(self):
        return IncidentMessage.objects.filter(incident_id=self.kwargs['incident_pk']).order_by('-created_at')

    def create(self, request, **kwargs):
        user = request.user if request.user.is_authenticated else None
        message_type = request.data.get("message_type")
        logger.info(
            "incident_message_create_requested",
            incident_id=self.kwargs["incident_pk"],
            requested_by_user_id=user.id if user else None,
            message_type=message_type,
        )

        msg = IncidentMessage.objects.create(
            user=user,
            message_type=message_type,
            incident_id=self.kwargs['incident_pk'],
            content_type=None,
            object_id=None
        )

        serializer_class = {
            IncidentMessage.TEXT: TextMessageSerializer,
            IncidentMessage.PHOTO: PhotoMessageSerializer,
            IncidentMessage.VIDEO: VideoMessageSerializer,
            IncidentMessage.AUDIO: AudioMessageSerializer,
        }.get(message_type)

        if not serializer_class:
            logger.warning(
                "incident_message_create_invalid_type",
                incident_id=self.kwargs["incident_pk"],
                requested_by_user_id=user.id if user else None,
                message_type=message_type,
            )
            return Response({"error": "Invalid message type"}, status=status.HTTP_400_BAD_REQUEST)

        serializer = serializer_class(data=request.data)
        if serializer.is_valid():
            content_obj = serializer.save(message=msg)
            msg.content_type = ContentType.objects.get_for_model(content_obj)
            msg.object_id = content_obj.id
            msg.save()
            logger.info(
                "incident_message_created",
                incident_id=self.kwargs["incident_pk"],
                incident_message_id=msg.id,
                requested_by_user_id=user.id if user else None,
                message_type=message_type,
                content_object_id=content_obj.id,
                text=getattr(content_obj, "text", None),
                file_name=(
                    getattr(getattr(content_obj, "photo", None), "name", None)
                    or getattr(getattr(content_obj, "video", None), "name", None)
                    or getattr(getattr(content_obj, "audio", None), "name", None)
                ),
            )
            incident = Incident.objects.get(pk=self.kwargs['incident_pk'])
            if incident.point:
                author_name = (request.user.display_name if request.user.is_authenticated else "Система")
                notification_text = None
                if message_type == IncidentMessage.TEXT and content_obj:
                    raw_text = getattr(content_obj, "text", "")
                    raw_text = raw_text.strip().replace("\n", " ")
                    if raw_text:
                        prefix = f"Комментарий от {author_name}: "
                        max_preview_len = 255 - len(prefix)
                        if max_preview_len < 0:
                            max_preview_len = 0
                        preview = raw_text
                        if len(preview) > max_preview_len:
                            if max_preview_len > 1:
                                preview = preview[: max_preview_len - 1] + "…"
                            else:
                                preview = preview[:max_preview_len]
                        notification_text = prefix + preview

                if not notification_text:
                    type_label = {
                        IncidentMessage.TEXT: "текст",
                        IncidentMessage.PHOTO: "фото",
                        IncidentMessage.VIDEO: "видео",
                        IncidentMessage.AUDIO: "аудио",
                    }.get(message_type, "сообщение")
                    notification_text = f"Новый комментарий ({type_label}) от {author_name}."

                if len(notification_text) > 255:
                    notification_text = notification_text[:254] + "…"
                notify_duty_point_participants(
                    incident.point,
                    incident.name,
                    notification_text,
                    NotificationSourceEnum.DISPATCH.value,
                )
            return Response(IncidentMessageSerializer(msg).data, status=status.HTTP_201_CREATED)

        msg.delete()
        logger.warning(
            "incident_message_create_validation_failed",
            incident_id=self.kwargs["incident_pk"],
            requested_by_user_id=user.id if user else None,
            message_type=message_type,
            errors=serializer.errors,
        )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
