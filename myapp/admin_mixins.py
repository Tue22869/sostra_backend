from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin


class CustomAdmin(SimpleHistoryAdmin):
       
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        extra_context['show_save'] = False  # Убираем кнопку "Сохранить"
        extra_context['show_save_and_add_another'] = False  # Убираем кнопку "Сохранить и добавить ещё"
        extra_context['show_save_and_continue'] = True  # Оставляем только "Сохранить и продолжить редактирование"
        return super().changeform_view(request, object_id, form_url, extra_context)
