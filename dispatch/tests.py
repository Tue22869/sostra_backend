from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from dispatch.admin import ClearDutyForm, DutyAdminForm, DutyForm
from dispatch.models import Duty, DutyAction, DutyActionTypeEnum, DutyRole
from dispatch.views import DutyViewSet
from dispatch.utils import now, today
from users.models import Notification, NotificationSourceEnum, User


class DutyProtectionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
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
