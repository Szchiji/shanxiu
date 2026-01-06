# -*- encoding: utf-8 -*-
"""
URL configuration for shanxiu project.
"""

from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('authentication.urls')),
    path('', include('app.urls')),
]
