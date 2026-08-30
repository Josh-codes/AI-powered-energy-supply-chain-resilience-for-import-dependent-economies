"""
Django settings for the Energy Supply Chain Resilience System.

Every secret and deployment-specific value is read from the environment
(or backend/.env via django-environ). Nothing sensitive is hardcoded.
"""
import os
from pathlib import Path

import environ

# backend/ — the directory that holds manage.py
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:3000"]),
    DB_PORT=(int, 5432),
)

# Load backend/.env if present (it is git-ignored).
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    env.read_env(str(_env_file))

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="django-insecure-dev-only-change-me")
DEBUG = env.bool("DEBUG", default=True)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
    "django_celery_beat",
]

LOCAL_APPS = [
    "core.apps.CoreConfig",
    "graph.apps.GraphConfig",
    "pipeline.apps.PipelineConfig",
    "criticality.apps.CriticalityConfig",
    "response.apps.ResponseConfig",
    "orchestrator.apps.OrchestratorConfig",
    "backtest.apps.BacktestConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # CorsMiddleware must come before CommonMiddleware.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Native geospatial libraries (GDAL / GEOS / PROJ)
# ---------------------------------------------------------------------------
# On Windows with a venv install (GDAL wheel), point these at the DLLs that
# ship inside site-packages/osgeo/ — see .env.example. Leave unset on Linux/
# macOS where the system packages are found automatically.
_gdal_lib = env("GDAL_LIBRARY_PATH", default="")
_geos_lib = env("GEOS_LIBRARY_PATH", default="")
_proj_lib = env("PROJ_LIB", default="")

if _gdal_lib:
    GDAL_LIBRARY_PATH = _gdal_lib
if _geos_lib:
    GEOS_LIBRARY_PATH = _geos_lib
if _proj_lib:
    os.environ.setdefault("PROJ_LIB", _proj_lib)

if os.name == "nt":
    for _lib in (_gdal_lib, _geos_lib):
        _dir = os.path.dirname(_lib)
        if _dir and os.path.isdir(_dir):
            try:
                os.add_dll_directory(_dir)
            except OSError:
                pass

# ---------------------------------------------------------------------------
# Database — PostgreSQL + PostGIS
# ---------------------------------------------------------------------------
# Provide either a full DATABASE_URL (postgis://user:pass@host:port/name)
# or the discrete DB_* vars below (see .env.example).
if env("DATABASE_URL", default=""):
    DATABASES = {"default": env.db("DATABASE_URL")}
    DATABASES["default"].setdefault(
        "ENGINE", "django.contrib.gis.db.backends.postgis"
    )
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "NAME": env("DB_NAME", default="energy_resilience"),
            "USER": env("DB_USER", default="postgres"),
            "PASSWORD": env("DB_PASSWORD", default=""),
            "HOST": env("DB_HOST", default="localhost"),
            "PORT": env.int("DB_PORT", default=5432),
        }
    }

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_PAGINATION_CLASS": None,
}
if DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"].append(
        "rest_framework.renderers.BrowsableAPIRenderer"
    )

# ---------------------------------------------------------------------------
# CORS (frontend origin)
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS", default=["http://localhost:3000"]
)
CORS_ALLOW_CREDENTIALS = False

# ---------------------------------------------------------------------------
# Celery / Redis
# ---------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

CELERY_BEAT_SCHEDULE = {
    "poll-gdelt": {
        "task": "pipeline.tasks.poll_gdelt",
        "schedule": 21600,          # every 6 hours
    },
    "poll-rss": {
        "task": "pipeline.tasks.poll_rss",
        "schedule": 21600,
    },
    "extract-events": {
        "task": "pipeline.tasks.extract_events",
        "schedule": 21600,
    },
    "score-and-update": {
        "task": "pipeline.tasks.score_and_update_graph",
        "schedule": 21600,
    },
    "run-criticality": {
        "task": "pipeline.tasks.run_criticality",
        "schedule": 21600,
    },
    "download-ofac": {
        "task": "pipeline.tasks.download_ofac",
        "schedule": 604800,         # weekly
    },
}

# ---------------------------------------------------------------------------
# Logging  (project rule: use logging, never print, for errors)
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname:<8} {name} | {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "pipeline": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "graph": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "criticality": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "response": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "orchestrator": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "backtest": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# External API keys / model config (read where needed, never hardcode)
# ---------------------------------------------------------------------------
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_MODEL = env("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_MAX_TOKENS = env.int("OPENAI_MAX_TOKENS", default=500)
EIA_API_KEY = env("EIA_API_KEY", default="")
