from django.apps import AppConfig


class GemstoneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gemstone"

    def ready(self):
        from . import worker

        if worker.should_start():
            worker.start()
