from django.apps import AppConfig


class ResponseConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "response"
    verbose_name = "Response optimization (reroute / SPR)"
