"""Config package.

Import the Celery app so shared_task can find it and ``celery -A config``
works, per the project spec.
"""
from .celery import app as celery_app

__all__ = ("celery_app",)
