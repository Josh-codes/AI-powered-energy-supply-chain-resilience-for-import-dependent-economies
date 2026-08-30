"""Celery application for the Energy Supply Chain Resilience System.

Task definitions live in ``pipeline/tasks.py`` (and any other app ``tasks.py``);
``autodiscover_tasks`` picks them up from every app in ``INSTALLED_APPS``.
The periodic schedule (``CELERY_BEAT_SCHEDULE``) is added in the pipeline phase.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("energy_resilience")

# Read config from Django settings, keys prefixed with CELERY_.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load tasks.py from every installed app.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Trivial task to verify the worker is wired up."""
    print(f"Request: {self.request!r}")
