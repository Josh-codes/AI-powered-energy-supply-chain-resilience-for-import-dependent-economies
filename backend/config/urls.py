"""Root URL configuration.

All application endpoints are mounted under /api/ via core.urls.
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
]
