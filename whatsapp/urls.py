from django.urls import path

from whatsapp.views import WhatsAppActivityView

urlpatterns = [
    path("whatsapp/", WhatsAppActivityView.as_view(), name="whatsapp"),
]
