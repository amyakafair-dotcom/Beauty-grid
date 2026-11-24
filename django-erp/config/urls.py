from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

from erp import assistant, store, views

urlpatterns = [
    path("", views.home, name="home"),
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),  # language switch (set_language)
    path("login/", auth_views.LoginView.as_view(template_name="erp/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", views.signup, name="signup"),
    path("team/", views.team, name="team"),
    path("pos/", views.pos, name="pos"),
    path("pos/order/", views.pos_order, name="pos_order"),
    path("invoice/<int:pk>/", views.invoice, name="invoice"),
    path("receipt/<int:pk>/", views.receipt, name="receipt"),
    path("reports/", views.reports, name="reports"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("owner/", views.owner, name="owner"),
    path("webhooks/meta/", assistant.webhook, name="meta_webhook"),
    path("shop/<str:slug>/", store.shop, name="shop"),
    path("shop/<str:slug>/cart/", store.cart, name="cart"),
    path("shop/<str:slug>/cart/update/", store.cart_update, name="cart_update"),
    path("shop/<str:slug>/order/<int:pk>/", store.order_done, name="order_done"),
    # product photos; fine for a small store, move to a CDN/bucket when traffic grows
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
