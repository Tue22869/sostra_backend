from django.apps import AppConfig


class DispatchConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dispatch'
    verbose_name = 'Приложение диспетчеризации'

    def ready(self):
        from dispatch.audit import register_dispatch_audit_signals

        register_dispatch_audit_signals()
