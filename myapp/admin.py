import enum
import os
import urllib.parse

from django import forms
from django.contrib import admin
from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin, GroupAdmin
from django.contrib.auth.forms import SetPasswordForm, UserCreationForm
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import path
from django.urls import reverse
from django.utils.html import format_html
from simple_history.admin import SimpleHistoryAdmin

from dispatch.admin import register_dispatch_admin
from food.admin import register_food_admin
from users.admin import register_user_admin
from food.models import Feedback
from myapp.admin_mixins import CustomAdmin
from myapp.custom_groups import UserManager, SeniorUserManager, CanteenAdminManager
from myapp.excel import fire_extinguishers, guards_stats
from myapp.models import Guard, Round, Visit, Point, Message, Device
from myapp.scheduler_admin import register_scheduler_admin
from myapp.services.guards import get_manager_guards, get_guard_by_guard_id
from myapp.services.messages import messages_by_user
from myproject.settings import AUTH_USER_MODEL


class ServicesEnum(enum.StrEnum):
    QR_GUARD = 'myapp'
    CANTEEN = 'food'


class GuardsStatsForm(forms.Form):
    ALL_EMPLOYEES_OPTION = -1

    guards = forms.ChoiceField(
        choices=[],
        label="Выберите сотрудника",
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-control',  # Добавляем Bootstrap-класс
            'placeholder': 'Выберите сотрудника'
        })
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)
        guards_choices = [(self.ALL_EMPLOYEES_OPTION, "Все сотрудники")] + [
            (employee.id, employee.name) for employee in get_manager_guards(self.request.user)
        ]
        self.fields['guards'].choices = guards_choices

    def get_guards(self):
        guard_id = int(self.cleaned_data['guards'])
        return get_manager_guards(self.request.user) if guard_id == self.ALL_EMPLOYEES_OPTION else [
            get_guard_by_guard_id(guard_id)]


class GroupUserManagementForm(forms.Form):
    add_user = forms.ModelChoiceField(
        queryset=get_user_model().objects,
        required=False,
        label="Добавить в сервис",
    )

    def __init__(self, *args, **kwargs):
        self.group = kwargs.pop('group', None)
        super().__init__(*args, **kwargs)
        self.fields['add_user'].queryset = get_user_model().objects.exclude(groups=self.group)

    def save(self):
        new_group_user = self.cleaned_data['add_user']
        self.group.user_set.add(new_group_user)
        self.group.save()


class MyAdminSite(AdminSite):
    def register(self, model_or_iterable, admin_class=None, **options):
        if admin_class is None:
            admin_class = CustomAdmin
        super().register(model_or_iterable, admin_class, **options)

    def app_index(self, request, app_label, extra_context=None):
        extra_context = extra_context or {}
        if app_label == 'food':
            extra_context['unread_feedback_count'] = Feedback.objects.filter(is_read=False).count()
        return super().app_index(request, app_label, extra_context)

    def get_absolute_url(self, request, view_name, *args, **kwargs):
        return os.path.join(f'http://{request.get_host()}', 'admin', view_name)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('export-fire-extinguishers/', fire_extinguishers, name='export_fire_extinguishers'),
            path('export-guards-stats/', self.guards_stats_form, name='guards_stats'),
            path('manage_group_users/<str:group_name>', self.manage_group_users, name='manage_group_users'),
            path('manage_group_users/<str:group_name>/delete/<str:user_id>', self.manage_group_users_delete,
                 name='manage_group_users_delete'),
        ]
        return custom_urls + urls

    def guards_stats_form(self, request):
        if request.method == 'POST':
            form = GuardsStatsForm(request.POST, request=request)
            if form.is_valid():
                guards = form.get_guards()
                return guards_stats(guards)
        else:
            form = GuardsStatsForm(request=request)

        return render(request, 'guards_stats.html', {'form': form})

    def manage_group_users(self, request, group_name):
        group = get_object_or_404(Group, name=group_name)
        if request.method == 'POST':
            form = GroupUserManagementForm(request.POST, group=group)
            if form.is_valid():
                form.save()
        else:
            form = GroupUserManagementForm(group=group)

        return render(request, 'manage_group_users.html',
                      {'form': form, 'group': group, 'users_in_group': group.user_set.all()})

    def manage_group_users_delete(self, request, group_name, user_id):
        if request.method == 'POST':
            group = get_object_or_404(Group, name=group_name)
            user = get_object_or_404(AUTH_USER_MODEL, id=user_id)
            group.user_set.remove(user)
            return redirect(reverse('admin:manage_group_users', kwargs={'group_name': group_name}))

    def get_buttons(self, request, app_name):
        if app_name == ServicesEnum.QR_GUARD.value:
            result = [
                {
                    'label': 'Выгрузить информацию об огнетушителях',
                    'url': self.get_absolute_url(request, 'export-fire-extinguishers/'),
                },
                {
                    'label': 'Выгрузить информацию об обходах',
                    'url': self.get_absolute_url(request, 'export-guards-stats/'),
                }
            ]
            return result

        if app_name == ServicesEnum.CANTEEN:
            if request.user.is_superuser or request.user.groups.filter(name=CanteenAdminManager.name).exists():
                return [{
                    'label': 'Добавить сотрудников в сервис',
                    'url': self.get_absolute_url(request, 'manage_group_users/canteen_employee'),
                }]

        return []

    def each_context(self, request):
        app_name = request.path.split('/')[2] or None
        extra_context = super().each_context(request)
        extra_context['custom_buttons'] = self.get_buttons(request, app_name)
        extra_context['is_index'] = request.path.endswith('/admin/')
        extra_context['message_count'] = len(messages_by_user(request.user))
        extra_context['app_name'] = app_name
        return extra_context


class VisitInline(admin.StackedInline):
    model = Visit
    extra = 0
    can_delete = False
    max_num = 0
    ordering = ('-created_at',)
    readonly_fields = ['point', 'created_at']


class GuardAdmin(CustomAdmin):
    exclude = ['name_old']
    actions = ['manager_delete']

    def has_super_permission(self, request):
        return request.user.is_superuser or request.user.groups.filter(
            name='Senior Managers').exists() or request.user.groups.filter(name='senior_user_manager').exists()

    def has_manager_permission(self, request):
        return request.user.groups.filter(name='Managers').exists() or request.user.groups.filter(
            name='qr_manager').exists()

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if self.has_super_permission(request):
            return qs
        elif self.has_manager_permission(request):
            return qs.filter(managers=request.user)
        return []

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        if self.has_super_permission(request):
            return True
        return False

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        if self.has_super_permission(request):
            return True
        return False

    def manager_delete(self, request, queryset):
        for item in queryset:
            item.managers.remove(request.user)
            self.update_qr_guard_group(item)

    manager_delete.short_description = "Отвязать сотрудника от себя"

    def get_actions(self, request):
        actions = super().get_actions(request)

        if self.has_delete_permission(request):
            del actions['manager_delete']

        return actions

    def update_qr_guard_group(self, obj):
        if not obj.managers.all().exists():
            if obj.user:
                group = get_object_or_404(Group, name='qr_guard')
                group.user_set.remove(obj.user)
        else:
            if obj.user:
                group = get_object_or_404(Group, name='qr_guard')
                group.user_set.add(obj.user)

    def save_model(self, request, obj, form, change):
        if Guard.objects.filter(user=obj.user).exists():
            guard = Guard.objects.get(user=obj.user)
            if not self.has_super_permission(request):
                guard.managers.add(request.user)
            else:
                for item in form.cleaned_data['managers']:
                    guard.managers.add(item)
            guard.save()
            obj.id = guard.id  # самое легкое решение, чтобы перенаправить на уже существующий объект после создания
        else:
            super().save_model(request, obj, form, change)

        self.update_qr_guard_group(obj)

    def get_readonly_fields(self, request, obj=None):
        if self.has_super_permission(request):
            if obj is not None and not request.user.is_superuser:
                return ['user']
            return []
        return ['managers']


class RoundAdmin(CustomAdmin):
    list_filter = [
        "guard",
    ]

    def is_finished(self, obj):
        return not obj.is_active

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser or request.user.groups.filter(
                name='Senior Managers').exists() or request.user.groups.filter(name='senior_user_manager').exists():
            return qs
        if request.user.groups.filter(name='Managers').exists() or request.user.groups.filter(
                name='qr_manager').exists():
            return qs.filter(guard__managers=request.user)
        return []

    inlines = [VisitInline]


class VisitAdmin(CustomAdmin):
    list_filter = [
        "point",
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser or request.user.groups.filter(
                name='Senior Managers').exists() or request.user.groups.filter(name='senior_user_manager').exists():
            return qs
        if request.user.groups.filter(name='Managers').exists() or request.user.groups.filter(
                name='qr_manager').exists():
            return qs.filter(round__guard__managers=request.user)
        return []


def show_qr_code(request, object_id):
    return generate_qr_code(request, object_id, False)


def download_qr_code(request, object_id):
    return generate_qr_code(request, object_id, True)


def generate_qr_code(request, object_id, force_download=True):
    obj = get_object_or_404(Point, pk=object_id)
    qr_code_file = obj.generate_qr_code()

    filename = f"{obj.name} QR.png"
    encoded_filename = urllib.parse.quote(filename)

    if force_download:
        response = HttpResponse(qr_code_file, content_type='application/force-download')
        response['Content-Disposition'] = f'attachment; filename*=UTF-8\'\'{encoded_filename}'
    else:
        response = HttpResponse(qr_code_file, content_type='image/png')
    return response


class PointAdmin(CustomAdmin):
    list_display = ['name', 'qr_code_button']
    search_fields = ['name__icontains']

    def qr_code_button(self, obj):
        return format_html('<a class="button" href="{}">Показать</a>&nbsp;<a class="button" href="{}">Скачать</a>',
                           f'/admin/myapp/point/{obj.pk}/qr_code/show/',
                           f'/admin/myapp/point/{obj.pk}/qr_code/download/')

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<int:object_id>/qr_code/show/', self.admin_site.admin_view(show_qr_code), name='show-qr-code'),
            path('<int:object_id>/qr_code/download/', self.admin_site.admin_view(download_qr_code),
                 name='download-qr-code'),
        ]
        return custom_urls + urls

    qr_code_button.short_description = 'QR Код'


class MessageAdmin(CustomAdmin):
    list_display = ['visit', 'text', 'is_seen']
    readonly_fields = ['guard', 'visit']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser or request.user.groups.filter(
                name='Senior Managers').exists() or request.user.groups.filter(name='senior_user_manager').exists():
            return qs
        if request.user.groups.filter(name='Managers').exists() or request.user.groups.filter(
                name='qr_manager').exists():
            return qs.filter(guard__managers=request.user)
        return []


def is_user_manager(user):
    return user.groups.filter(name=UserManager.name).exists() or user.groups.filter(
        name=SeniorUserManager.name).exists()


def is_senior_user_manager(user):
    return user.groups.filter(name=SeniorUserManager.name).exists()


def user_has_group(user, group):
    return user.groups.filter(name=group.name).exists()


class CustomUserCreationForm(UserCreationForm):
    phone = forms.CharField(
        max_length=20,
        required=True,
        label="Номер телефона",
        help_text="Формат: +7XXXXXXXXXX",
        widget=forms.TextInput(attrs={'placeholder': '+7XXXXXXXXXX'})
    )

    class Meta:
        model = get_user_model()
        fields = ('username', 'first_name', 'last_name', 'phone')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['phone'].required = True

    def clean_phone(self):
        phone = self.cleaned_data.get('phone')
        if not phone:
            raise forms.ValidationError("Номер телефона обязателен для новых пользователей")
        return phone


class CustomUserAdmin(SimpleHistoryAdmin, UserAdmin):
    add_form = CustomUserCreationForm
    
    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': ('username', 'password1', 'password2', 'first_name', 'last_name', 'phone', 'is_staff', 'must_change_password', 'telegram_user_id'),
            },
        ),
    )

    def get_fieldsets(self, request, obj=None):
        if not obj:
            return self.add_fieldsets

        if not request.user.is_superuser:
            return ((None, {'fields': ('username', 'first_name', 'last_name', 'phone', 'is_staff', 'groups', 'must_change_password', 'telegram_user_id')}),)
        else:
            fieldsets = list(super().get_fieldsets(request, obj))
            first_fields = list(fieldsets[0][1]['fields'])
            if 'must_change_password' not in first_fields:
                first_fields.append('must_change_password')
            if 'telegram_user_id' not in first_fields:
                first_fields.append('telegram_user_id')
            if 'phone' not in first_fields:
                first_fields.append('phone')
            fieldsets[0][1]['fields'] = tuple(first_fields)
            return fieldsets

    def get_readonly_fields(self, request, obj=None):
        if not request.user.is_superuser and not is_senior_user_manager(request.user):
            return 'is_superuser', 'user_permissions', 'groups', 'telegram_user_id'
        return super().get_readonly_fields(request, obj)

    def has_add_permission(self, request):
        return request.user.is_superuser or is_user_manager(request.user)

    def has_change_permission(self, request, obj=None):
        if not request.user.is_superuser and obj and obj.is_superuser and not is_user_manager(request.user):
            return False
        return super().has_change_permission(request, obj)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            return qs.filter(is_superuser=False)
        return qs

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser or is_user_manager(request.user)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        user = get_object_or_404(get_user_model(), pk=object_id)
        change_password_url = reverse('admin:auth_user_password_change', args=[user.id])

        extra_context = extra_context or {}
        extra_context['change_password_url'] = change_password_url

        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def get_urls(self):
        from django.urls import path
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:user_id>/password/',
                self.admin_site.admin_view(self.change_password),
                name='auth_user_password_change',
            ),
        ]
        return custom_urls + urls

    def change_password(self, request, user_id):
        user_to_change = get_user_model().objects.get(pk=user_id)

        if not (request.user.is_superuser or
                request.user.groups.filter(name='senior_user_manager').exists() or
                request.user == user_to_change):
            raise PermissionDenied("У вас нет прав для смены этого пароля.")

        if request.method == 'POST':
            form = SetPasswordForm(user_to_change, request.POST)
            if form.is_valid():
                form.save()
                return redirect('admin:users_user_change', user_id)
        else:
            form = SetPasswordForm(user_to_change)

        return render(request, 'admin/users/user/change_password.html', {
            'form': form,
            'user_to_change': user_to_change,
        })


admin.site = MyAdminSite()

admin.site.register(Guard, GuardAdmin)
admin.site.register(Round, RoundAdmin)
admin.site.register(Visit, VisitAdmin)
admin.site.register(Point, PointAdmin)
admin.site.register(Message, MessageAdmin)
admin.site.register(get_user_model(), CustomUserAdmin, ordering=get_user_model()._meta.ordering)


class HistoryGroupAdmin(SimpleHistoryAdmin, GroupAdmin):
    pass


admin.site.register(Group, HistoryGroupAdmin)
admin.site.register(Device)
register_food_admin(admin.site)
register_dispatch_admin(admin.site)
register_user_admin(admin.site)
register_scheduler_admin(admin.site)
