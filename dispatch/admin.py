import calendar
from datetime import date, timedelta
from django import forms
from django.contrib import admin
from django.contrib.admin import AdminSite
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html

from dispatch.models import DutyPoint, DutyRole, Duty, IncidentMessage, TextMessage, VideoMessage, PhotoMessage, \
    AudioMessage, Incident, ExploitationRole, IncidentStatusEnum
from dispatch.services.incident_statistics import get_incident_statistics
from dispatch.services.duties import (
    get_duties_by_date,
    get_duties_assigned,
    get_duties_covering_date,
    get_or_create_duty,
    get_or_create_duty_range,
    duty_overlaps_range,
    delete_duty,
)
from dispatch.calendar_ru import get_non_working_ranges
from dispatch.utils import decl, now, today
from myapp.admin_mixins import CustomAdmin
from myapp.services.users import get_all_users


class DispatchAdmin(AdminSite):
    site_header = "Кастомная админка"
    site_title = "Панель управления"
    index_title = "Добро пожаловать в админку"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("", self.index_page, name="index"),
            path("duty-schedule/", self.schedule_page, name="schedule"),
        ]
        return custom_urls + urls

    def index_page(self, request):
        return render(request, "admin/dispatch/index.html")

    def schedule_page(self, request):
        return render(request, "admin/dispatch/schedule.html")


def get_calendar_data(year, month, role: DutyRole):
    cal = calendar.Calendar(firstweekday=0)  # Неделя начинается с понедельника
    month_days = cal.monthdayscalendar(year, month)
    current_datetime = now()

    calendar_weeks = []
    for week in month_days:
        formatted_week = []
        for day in week:
            if day != 0:
                day_date = date(year, month, day)
                duties = get_duties_covering_date(day_date, role)

                colored_duties = []
                for duty in duties:
                    # Не показываем в последний день дежурства
                    if day_date == duty.end_datetime.date():
                        continue
                    start_d = duty.start_datetime.date()
                    end_d = duty.end_datetime.date()
                    is_multiday = end_d > start_d
                    if not duty.is_opened:
                        status_color = '#dc3545'
                        status_label = 'Не принято'
                    elif duty.is_forced_opened:
                        status_color = '#d4a017'
                        status_label = 'Принято автоматически'
                    else:
                        status_color = '#2e8b57'
                        status_label = 'Принято дежурным'

                    if duty.start_datetime > current_datetime:
                        status_label = ''

                    colored_duties.append({
                        "id": duty.id,
                        "user": duty.user,
                        "color": status_color,
                        "status_label": status_label,
                        "start_date": start_d,
                        "end_date": end_d,
                        "start_datetime": duty.start_datetime,
                        "end_datetime": duty.end_datetime,
                        "is_multiday": is_multiday,
                        "change_url": reverse("admin:dispatch_duty_change", args=[duty.id]),
                    })

                formatted_week.append((day_date, colored_duties))
            else:
                formatted_week.append((None, None))
        calendar_weeks.append(formatted_week)

    return calendar_weeks


class DutyForm(forms.Form):
    user = forms.ModelChoiceField(queryset=get_all_users(), label="Сотрудник")
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), label="Дата начала")
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                               label="Дата окончания (Опционально)")
    duty_step = forms.IntegerField(required=False, min_value=1, label="Шаг дежурства (дней) (Опционально)")
    rest_step = forms.IntegerField(required=False, min_value=1, label="Шаг отдыха (дней) (Опционально)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        min_date = today().isoformat()
        self.fields['start_date'].widget.attrs['min'] = min_date
        self.fields['end_date'].widget.attrs['min'] = min_date

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if start_date and start_date < today():
            self.add_error('start_date', 'Нельзя назначать дежурства на прошедшие даты.')

        if start_date and end_date and end_date < start_date:
            self.add_error('end_date', 'Дата окончания не может быть раньше даты начала.')

        return cleaned_data


class ClearDutyForm(forms.Form):
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), label="Дата начала")
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                               label="Дата окончания")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        min_date = today().isoformat()
        self.fields['start_date'].widget.attrs['min'] = min_date
        self.fields['end_date'].widget.attrs['min'] = min_date

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if start_date and start_date < today():
            self.add_error('start_date', 'Нельзя изменять дежурства на прошедшие даты.')

        if start_date and end_date and end_date < start_date:
            self.add_error('end_date', 'Дата окончания не может быть раньше даты начала.')

        return cleaned_data


class DutyAdminForm(forms.ModelForm):
    class Meta:
        model = Duty
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        start_datetime = cleaned_data.get('start_datetime')
        if start_datetime is None:
            return cleaned_data

        start_date = start_datetime.date()
        current_date = today()

        if self.instance.pk:
            original = Duty.objects.filter(pk=self.instance.pk).only('start_datetime').first()
            if original and original.start_datetime.date() >= current_date and start_date < current_date:
                self.add_error(
                    'start_datetime',
                    'Нельзя переносить дежурство на прошедшую дату.',
                )
        elif start_date < current_date:
            self.add_error(
                'start_datetime',
                'Нельзя создавать дежурство на прошедшую дату.',
            )

        return cleaned_data


class ExploitationRoleAdmin(CustomAdmin):
    filter_horizontal = ('members',)


class DutyRoleAdmin(CustomAdmin):
    list_display = ['name', 'next_duty_stats', 'duty_schedule']

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<int:object_id>/schedule/', self.admin_site.admin_view(self.schedule), name='schedule-duty'),
        ]
        return custom_urls + urls

    def schedule(self, request, object_id):
        duty_role = DutyRole.objects.get(pk=object_id)

        duty_form = DutyForm()
        clear_duty_form = ClearDutyForm()

        if request.method == "POST" and request.POST.get("form_type") == "add_duty_form":
            duty_form = DutyForm(request.POST)
            if duty_form.is_valid():
                user = duty_form.cleaned_data["user"]
                start_date = duty_form.cleaned_data["start_date"]
                end_date = duty_form.cleaned_data["end_date"]
                if end_date is None:
                    end_date = start_date

                # Нерабочие дни по производственному календарю: объединяем в длинные дежурства
                ranges = get_non_working_ranges(start_date, end_date)
                non_working_days = set()
                for range_start, range_end in ranges:
                    current = range_start
                    while current <= range_end:
                        non_working_days.add(current)
                        current += timedelta(days=1)

                for range_start, range_end in ranges:
                    if duty_overlaps_range(duty_role, range_start, range_end):
                        continue
                    duty, created = get_or_create_duty_range(
                        range_start, range_end, duty_role, defaults={"user": user}
                    )
                    if not created:
                        duty.user = user
                        duty.save()

                # На рабочие дни продолжаем создавать обычные дежурства по шагу/отдыху
                duty_step = duty_form.cleaned_data.get("duty_step") or 1
                rest_step = duty_form.cleaned_data.get("rest_step") or 0
                current_date = start_date
                while current_date <= end_date:
                    for _ in range(duty_step):
                        if current_date > end_date:
                            break
                        if current_date not in non_working_days:
                            duty, created = get_or_create_duty(
                                duty_date=current_date, role=duty_role, defaults={"user": user}
                            )
                            if not created:
                                duty.user = user
                            duty.save()
                        current_date += timedelta(days=1)
                    current_date += timedelta(days=rest_step)
        elif request.method == "POST" and request.POST.get("form_type") == "clear_duty_form":
            clear_duty_form = ClearDutyForm(request.POST)
            if clear_duty_form.is_valid():
                start_date = clear_duty_form.cleaned_data["start_date"]
                end_date = clear_duty_form.cleaned_data["end_date"]
                if end_date is None:
                    end_date = start_date
                current_date = start_date
                while current_date <= end_date:
                    delete_duty(current_date, duty_role)
                    current_date += timedelta(days=1)

        today_date = today()
        year = int(request.GET.get('year', today_date.year))
        month = int(request.GET.get('month', today_date.month))

        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1

        context = {
            'current_year': year,
            'current_month': month,
            'current_month_name': month,
            'prev_year': prev_year,
            'prev_month': prev_month,
            'next_year': next_year,
            'next_month': next_month,
            'calendar_weeks': get_calendar_data(year, month, duty_role),
            'add_duty_form': duty_form,
            'clear_duty_form': clear_duty_form,
            'duty_role': duty_role,
        }

        return render(request, 'admin/dispatch/schedule.html', context)

    def duty_schedule(self, obj):
        return format_html('<a class="button" href="{}">Назначить график дежурств</a>',
                           f'/admin/dispatch/dutyrole/{obj.id}/schedule/')

    duty_schedule.short_description = 'Расписание дежурства'

    def next_duty_stats(self, obj):
        dt = today()
        num = get_duties_assigned(dt, obj)
        name = decl(num, ['день', 'дня', 'дней'])
        if num <= 7:
            color = '#dc3545'
            font_weight = 'bold'
        else:
            color = '#000000'
            font_weight = 'normal'
        return format_html(f'<a style="color: {color}; font-weight: {font_weight}">{num} {name}</a>')

    next_duty_stats.short_description = 'Кол-во распланированных дней'

class DutyAdmin(CustomAdmin):
    form = DutyAdminForm
    autocomplete_fields = (
        'notification_duty_is_coming',
        'notification_duty_reminder',
        'notification_need_to_open',
    )

    def get_exclude(self, request, obj=None):
        if request.user.is_superuser:
            return None
        return ('notification_need_to_open', 'notification_duty_is_coming')

    def has_change_permission(self, request, obj=None):
        has_perm = super().has_change_permission(request, obj)
        if not has_perm:
            return False
        if obj and obj.has_ended() and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            return False
        return has_perm

    def has_delete_permission(self, request, obj=None):
        has_perm = super().has_delete_permission(request, obj)
        if not has_perm:
            return False
        if obj and obj.has_ended():
            return False
        return has_perm

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.has_ended():
            readonly_fields.extend(field.name for field in self.model._meta.fields)
        return tuple(dict.fromkeys(readonly_fields))

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        obj = self.get_object(request, object_id) if object_id else None
        if obj and obj.has_ended() and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            raise PermissionDenied("Нельзя редактировать завершенное дежурство.")

        extra_context = extra_context or {}
        if obj and obj.has_ended():
            extra_context['show_save'] = False
            extra_context['show_save_and_add_another'] = False
            extra_context['show_save_and_continue'] = False
        else:
            extra_context.setdefault('show_save', False)
            extra_context.setdefault('show_save_and_add_another', False)
            extra_context.setdefault('show_save_and_continue', True)

        return admin.ModelAdmin.changeform_view(self, request, object_id, form_url, extra_context)


class IncidentAdmin(CustomAdmin):
    list_display = ('name', 'incident_chat_action', 'author', 'status', 'level', 'created_at',)
    readonly_fields = ('created_at',)
    change_list_template = 'admin/dispatch/incident/change_list.html'

    def incident_chat_action(self, obj):
        return format_html('<a class="button" style="color: green; background: none" href="{}">Открыть чат</a>',
                           f'/admin/dispatch/incident/{obj.id}/chat/')

    incident_chat_action.short_description = ''

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['show_statistics_button'] = True
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<int:object_id>/chat/', self.admin_site.admin_view(self.incident_chat), name='incident-chat'),
            path('statistics/', self.admin_site.admin_view(self.incident_statistics), name='incident-statistics'),
        ]
        return custom_urls + urls

    def incident_chat(self, request, object_id):
        incident = Incident.objects.get(pk=object_id)

        context = {
            'incident': incident,
        }

        return render(request, 'admin/dispatch/incident.html', context)

    def incident_statistics(self, request):
        """Страница статистики по инцидентам"""
        from django.contrib.auth import get_user_model
        from datetime import date
        
        statistics = None
        form_data = {}
        
        if request.method == 'POST':
            start_date_str = request.POST.get('start_date')
            end_date_str = request.POST.get('end_date')
            status = request.POST.get('status')
            responsible_user_id = request.POST.get('responsible_user_id')
            point_id = request.POST.get('point_id')
            author_id = request.POST.get('author_id')
            
            form_data = {
                'start_date': start_date_str,
                'end_date': end_date_str,
                'status': status,
                'responsible_user_id': responsible_user_id,
                'point_id': point_id,
                'author_id': author_id,
            }
            
            start_date = None
            end_date = None
            
            if start_date_str:
                try:
                    start_date = date.fromisoformat(start_date_str)
                except ValueError:
                    pass
            
            if end_date_str:
                try:
                    end_date = date.fromisoformat(end_date_str)
                except ValueError:
                    pass
            
            responsible_user_id_int = None
            if responsible_user_id:
                try:
                    responsible_user_id_int = int(responsible_user_id)
                except (ValueError, TypeError):
                    pass
            
            point_id_int = None
            if point_id:
                try:
                    point_id_int = int(point_id)
                except (ValueError, TypeError):
                    pass
            
            author_id_int = None
            if author_id:
                try:
                    author_id_int = int(author_id)
                except (ValueError, TypeError):
                    pass
            
            statistics = get_incident_statistics(
                start_date=start_date,
                end_date=end_date,
                status=status if status else None,
                responsible_user_id=responsible_user_id_int,
                point_id=point_id_int,
                author_id=author_id_int,
            )
        
        # Получаем данные для фильтров
        users = get_user_model().objects.all().order_by('last_name', 'first_name')
        points = DutyPoint.objects.all().order_by('name')
        status_choices = Incident.STATUS_CHOICES
        
        context = {
            'statistics': statistics,
            'form_data': form_data,
            'users': users,
            'points': points,
            'status_choices': status_choices,
        }
        
        return render(request, 'admin/dispatch/incident_statistics.html', context)


class DutyPointAdmin(CustomAdmin):
    filter_horizontal = ('admins',)


dispatch_admin_site = DispatchAdmin()


def register_dispatch_admin(site):
    site.register(DutyPoint, DutyPointAdmin)
    site.register(ExploitationRole, ExploitationRoleAdmin)
    site.register(DutyRole, DutyRoleAdmin)
    site.register(Duty, DutyAdmin)
    site.register(Incident, IncidentAdmin)
    site.register(IncidentMessage)
    site.register(TextMessage)
    site.register(VideoMessage)
    site.register(PhotoMessage)
    site.register(AudioMessage)
