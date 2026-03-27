from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework_simplejwt.tokens import RefreshToken

from dispatch.crons import check_missing_duties
from dispatch.admin import ClearDutyForm, DutyAdminForm, DutyForm
from dispatch.models import Duty, DutyAction, DutyActionTypeEnum, DutyPoint, DutyRole, Incident
from dispatch.services.duties import duty_overlaps_range
from dispatch.views import DutyViewSet
from dispatch.utils import now, today
from dispatch.models import IncidentStatusEnum
from users.models import Notification, NotificationSourceEnum, User


class DutyProtectionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.api_client = APIClient()
        self.role = DutyRole.objects.create(name="Test role")
        self.user = User.objects.create_user(username="duty-user", password="pass")
        self.other_user = User.objects.create_user(username="other-user", password="pass")
        self.superuser = User.objects.create_superuser(
            username="admin",
            password="pass",
            email="admin@example.com",
        )

    def test_schedule_forms_reject_past_dates(self):
        past_date = today() - timedelta(days=1)

        duty_form = DutyForm(data={"user": self.user.id, "start_date": past_date.isoformat()})
        clear_form = ClearDutyForm(data={"start_date": past_date.isoformat()})

        self.assertFalse(duty_form.is_valid())
        self.assertIn("start_date", duty_form.errors)
        self.assertFalse(clear_form.is_valid())
        self.assertIn("start_date", clear_form.errors)

    def test_duty_admin_form_rejects_new_past_duty(self):
        start_datetime = now() - timedelta(days=1)
        end_datetime = start_datetime + timedelta(hours=15)

        form = DutyAdminForm(
            data={
                "user": self.user.id,
                "role": self.role.id,
                "is_opened": False,
                "is_forced_opened": False,
                "start_datetime": start_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": end_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "notification_duty_is_coming": "",
                "notification_duty_reminder": "",
                "notification_need_to_open": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("start_datetime", form.errors)

    def test_cannot_save_ended_duty(self):
        ended_duty = Duty.objects.create(
            user=self.user,
            role=self.role,
            start_datetime=now() - timedelta(days=2),
            end_datetime=now() - timedelta(days=1, hours=1),
        )

        ended_duty.user = self.other_user

        with self.assertRaises(ValidationError):
            ended_duty.save()

    def test_open_endpoint_rejects_ended_duty(self):
        ended_duty = Duty.objects.create(
            user=self.user,
            role=self.role,
            start_datetime=now() - timedelta(days=2),
            end_datetime=now() - timedelta(days=1, hours=1),
        )

        request = self.factory.post(f"/api/dispatch/duties/{ended_duty.id}/open/")
        force_authenticate(request, user=self.user)
        response = DutyViewSet.as_view({"post": "open"})(request, pk=ended_duty.id)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Нельзя открыть завершённое дежурство")

    def test_transfer_endpoint_rejects_ended_duty(self):
        ended_duty = Duty.objects.create(
            user=self.user,
            role=self.role,
            start_datetime=now() - timedelta(days=2),
            end_datetime=now() - timedelta(days=1, hours=1),
        )

        request = self.factory.post(
            f"/api/dispatch/duties/{ended_duty.id}/transfer_duty/",
            {"user_id": self.other_user.id, "user_reason": "test"},
            format="json",
        )
        force_authenticate(request, user=self.user)
        response = DutyViewSet.as_view({"post": "transfer_duty"})(request, pk=ended_duty.id)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Нельзя изменять завершённое дежурство")

    def test_reassign_by_notification_rejects_ended_duty(self):
        ended_duty = Duty.objects.create(
            user=self.user,
            role=self.role,
            start_datetime=now() - timedelta(days=2),
            end_datetime=now() - timedelta(days=1, hours=1),
        )
        duty_action = DutyAction.objects.create(
            duty=ended_duty,
            user=self.user,
            action_type=DutyActionTypeEnum.TRANSFER.value,
            is_resolved=False,
        )
        notification = Notification.objects.create(
            user=self.superuser,
            title="test",
            text="test",
            source=NotificationSourceEnum.DISPATCH.value,
            duty_action=duty_action,
        )

        request = self.factory.post(
            "/api/dispatch/duties/reassign_by_notification/",
            {"notification_id": notification.id, "user_id": self.other_user.id},
            format="json",
        )
        force_authenticate(request, user=self.superuser)
        response = DutyViewSet.as_view({"post": "reassign_by_notification"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Нельзя изменять завершённое дежурство")

    def test_admin_post_rejects_ended_duty_changes(self):
        ended_duty = Duty.objects.create(
            user=self.user,
            role=self.role,
            start_datetime=now() - timedelta(days=2),
            end_datetime=now() - timedelta(days=1, hours=1),
        )

        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("admin:dispatch_duty_change", args=[ended_duty.id]),
            data={
                "user": self.other_user.id,
                "role": self.role.id,
                "is_opened": "on",
                "start_datetime_0": ended_duty.start_datetime.date().isoformat(),
                "start_datetime_1": ended_duty.start_datetime.strftime("%H:%M:%S"),
                "end_datetime_0": ended_duty.end_datetime.date().isoformat(),
                "end_datetime_1": ended_duty.end_datetime.strftime("%H:%M:%S"),
                "_save": "Сохранить",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_admin_response_contains_request_id_header(self):
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["X-Request-ID"])

    def test_duty_history_tracks_jwt_api_change(self):
        duty = Duty.objects.create(
            user=self.user,
            role=self.role,
            start_datetime=now() - timedelta(minutes=10),
            end_datetime=now() + timedelta(hours=8),
        )
        access_token = str(RefreshToken.for_user(self.user).access_token)

        response = self.api_client.post(
            f"/api/dispatch/duties/{duty.id}/open/",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
            HTTP_X_REQUEST_ID="req-duty-history",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Request-ID"], "req-duty-history")

        duty.refresh_from_db()
        history_record = duty.history.first()

        self.assertTrue(duty.is_opened)
        self.assertEqual(history_record.history_user, self.user)
        self.assertEqual(history_record.history_type, "~")
        self.assertTrue(history_record.is_opened)

        self.client.force_login(self.superuser)
        admin_history_response = self.client.get(
            reverse("admin:dispatch_duty_history", args=[duty.id])
        )
        self.assertEqual(admin_history_response.status_code, 200)

    def test_incident_history_tracks_jwt_api_change(self):
        incident = Incident.objects.create(
            name="Test incident",
            description="Test description",
            status=IncidentStatusEnum.OPENED.value,
            author=self.user,
            responsible_user=self.user,
        )
        access_token = str(RefreshToken.for_user(self.user).access_token)

        response = self.api_client.post(
            f"/api/dispatch/incidents/{incident.id}/change_status/",
            {"status": IncidentStatusEnum.CLOSED.value},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
            HTTP_X_REQUEST_ID="req-incident-history",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Request-ID"], "req-incident-history")

        incident.refresh_from_db()
        history_record = incident.history.first()

        self.assertEqual(incident.status, IncidentStatusEnum.CLOSED.value)
        self.assertEqual(history_record.history_user, self.user)
        self.assertEqual(history_record.history_type, "~")
        self.assertEqual(history_record.status, IncidentStatusEnum.CLOSED.value)


class MissingDutiesCronTests(TestCase):
    def setUp(self):
        self.user_index = 0

    def _create_user(self, prefix="duty-user"):
        self.user_index += 1
        return User.objects.create_user(
            username=f"{prefix}-{self.user_index}",
            password="pass",
        )

    def _create_point(self, name, level_1_name=None, level_2_name=None, level_3_name=None):
        roles = {}
        point_kwargs = {"name": name}

        for level, role_name in (
            (1, level_1_name),
            (2, level_2_name),
            (3, level_3_name),
        ):
            if role_name is None:
                continue
            role = DutyRole.objects.create(name=role_name)
            roles[level] = role
            point_kwargs[f"level_{level}_role"] = role

        point = DutyPoint.objects.create(**point_kwargs)
        return point, roles

    def _create_daily_duty(self, role, duty_date, user=None):
        user = user or self._create_user("daily-duty")
        return Duty.objects.create(
            user=user,
            role=role,
            start_datetime=datetime(
                duty_date.year,
                duty_date.month,
                duty_date.day,
                14,
                30,
                tzinfo=dt_timezone.utc,
            ),
            end_datetime=datetime(
                (duty_date + timedelta(days=1)).year,
                (duty_date + timedelta(days=1)).month,
                (duty_date + timedelta(days=1)).day,
                5,
                30,
                tzinfo=dt_timezone.utc,
            ),
        )

    def _create_range_duty(self, role, range_start, range_end, user=None):
        user = user or self._create_user("range-duty")
        duty_start_date = range_start - timedelta(days=1)
        day_after_end = range_end + timedelta(days=1)
        return Duty.objects.create(
            user=user,
            role=role,
            start_datetime=datetime(
                duty_start_date.year,
                duty_start_date.month,
                duty_start_date.day,
                14,
                30,
                tzinfo=dt_timezone.utc,
            ),
            end_datetime=datetime(
                day_after_end.year,
                day_after_end.month,
                day_after_end.day,
                5,
                30,
                tzinfo=dt_timezone.utc,
            ),
        )

    def _assign_daily_duties(self, role, start_date, end_date, user=None):
        user = user or self._create_user("assigned-duty")
        current_date = start_date
        while current_date <= end_date:
            self._create_daily_duty(role, current_date, user=user)
            current_date += timedelta(days=1)
        return user

    @patch("dispatch.crons.notify_point_admins")
    @patch("dispatch.crons.today", return_value=date(2026, 3, 28))
    def test_check_missing_duties_skips_multiday_duty_ranges(self, _today_mock, notify_point_admins_mock):
        point, roles = self._create_point("Водозаборный узел", level_1_name="Начальник ЛОС и ВЗУ")
        self._create_range_duty(roles[1], date(2026, 3, 28), date(2026, 3, 31))

        check_missing_duties()

        notify_point_admins_mock.assert_not_called()

    @patch("dispatch.crons.notify_point_admins")
    @patch("dispatch.crons.today", return_value=date(2026, 3, 25))
    def test_check_missing_duties_skips_notification_when_all_days_are_filled(
        self,
        _today_mock,
        notify_point_admins_mock,
    ):
        point, roles = self._create_point(
            "ЛОС производственных стоков",
            level_1_name="Начальник ЛОС и ВЗУ",
            level_2_name="Дежурный сантехник ЛОС и ВЗУ",
            level_3_name="Главный энергетик",
        )
        start_date = date(2026, 3, 25)
        end_date = date(2026, 3, 28)

        for role in roles.values():
            self._assign_daily_duties(role, start_date, end_date)

        check_missing_duties()

        notify_point_admins_mock.assert_not_called()

    @patch("dispatch.crons.notify_point_admins")
    @patch("dispatch.crons.today", return_value=date(2026, 3, 25))
    def test_check_missing_duties_sends_notification_for_missing_role_on_one_day(
        self,
        _today_mock,
        notify_point_admins_mock,
    ):
        point, roles = self._create_point(
            "ЛОС производственных стоков",
            level_1_name="Начальник ЛОС и ВЗУ",
            level_2_name="Дежурный сантехник ЛОС и ВЗУ",
            level_3_name="Главный энергетик",
        )
        start_date = date(2026, 3, 25)
        end_date = date(2026, 3, 28)

        self._assign_daily_duties(roles[1], start_date, end_date)
        self._assign_daily_duties(roles[3], start_date, end_date)
        missing_role_user = self._create_user("missing-role")
        # Последний день больше не считается покрытым целиком, если смена заканчивается утром.
        # Поэтому для реального пропуска 27.03 оставляем смены на 25.03, 26.03 и 28.03.
        self._create_daily_duty(roles[2], date(2026, 3, 25), user=missing_role_user)
        self._create_daily_duty(roles[2], date(2026, 3, 26), user=missing_role_user)
        self._create_daily_duty(roles[2], date(2026, 3, 28), user=missing_role_user)

        check_missing_duties()

        notify_point_admins_mock.assert_called_once()
        args = notify_point_admins_mock.call_args.args
        self.assertEqual(args[0], point)
        self.assertEqual(args[1], f"Отсутствуют дежурства в системе {point.name}")
        self.assertEqual(
            args[2],
            "В ближайшие 3 дня не назначены дежурства:\n27.03.2026 - Дежурный сантехник ЛОС и ВЗУ",
        )
        self.assertEqual(args[3], NotificationSourceEnum.DISPATCH.value)

    @patch("dispatch.crons.notify_point_admins")
    @patch("dispatch.crons.today", return_value=date(2026, 3, 25))
    def test_check_missing_duties_notifies_only_points_with_missing_duties(
        self,
        _today_mock,
        notify_point_admins_mock,
    ):
        complete_point, complete_roles = self._create_point(
            "Водозаборный узел",
            level_1_name="Начальник ЛОС и ВЗУ",
            level_2_name="Дежурный оператор ВЗУ",
            level_3_name="Главный энергетик",
        )
        missing_point, missing_roles = self._create_point(
            "Система канализации",
            level_1_name="Начальник ЛОС и ВЗУ",
            level_2_name="Дежурный сантехник ЛОС и ВЗУ",
            level_3_name="Главный энергетик",
        )
        start_date = date(2026, 3, 25)
        end_date = date(2026, 3, 28)

        for role in complete_roles.values():
            self._assign_daily_duties(role, start_date, end_date)

        self._assign_daily_duties(missing_roles[1], start_date, end_date)
        self._assign_daily_duties(missing_roles[2], start_date, end_date)
        self._assign_daily_duties(missing_roles[3], start_date, date(2026, 3, 26))

        check_missing_duties()

        notify_point_admins_mock.assert_called_once()
        args = notify_point_admins_mock.call_args.args
        self.assertEqual(args[0], missing_point)
        self.assertIn("28.03.2026 - Главный энергетик", args[2])

    @patch("dispatch.crons.notify_point_admins")
    @patch("dispatch.crons.today", return_value=date(2026, 3, 26))
    def test_check_missing_duties_does_not_count_end_day_covered_by_morning_finish(
        self,
        _today_mock,
        notify_point_admins_mock,
    ):
        point, roles = self._create_point(
            "Водозаборный узел",
            level_1_name="Начальник ЛОС и ВЗУ",
        )
        role = roles[1]
        user = self._create_user("edge-case")

        self._create_daily_duty(role, date(2026, 3, 25), user=user)
        self._create_daily_duty(role, date(2026, 3, 27), user=user)
        self._create_daily_duty(role, date(2026, 3, 28), user=user)
        self._create_daily_duty(role, date(2026, 3, 29), user=user)

        check_missing_duties()

        notify_point_admins_mock.assert_called_once()
        args = notify_point_admins_mock.call_args.args
        self.assertEqual(args[0], point)
        self.assertEqual(
            args[2],
            "В ближайшие 3 дня не назначены дежурства:\n26.03.2026 - Начальник ЛОС и ВЗУ",
        )

    def test_duty_overlaps_range_does_not_treat_morning_end_as_overlap_for_start_day(self):
        _, roles = self._create_point(
            "Водозаборный узел",
            level_1_name="Начальник ЛОС и ВЗУ",
        )
        role = roles[1]
        user = self._create_user("overlap-case")

        self._create_daily_duty(role, date(2026, 3, 25), user=user)

        self.assertFalse(duty_overlaps_range(role, date(2026, 3, 26), date(2026, 3, 28)))
