def arabic_first(get_response):
    """Start everyone in Arabic; the language button (cookie) still switches to English."""
    def middleware(request):
        request.META.pop("HTTP_ACCEPT_LANGUAGE", None)
        return get_response(request)
    return middleware


def block_inactive(get_response):
    """A switched-off company can't use the system."""
    from django.contrib.auth import logout
    from django.shortcuts import redirect

    def middleware(request):
        profile = getattr(request.user, "profile", None) if request.user.is_authenticated else None
        if profile and not profile.company.active:
            logout(request)
            return redirect("/login/?inactive=1")
        return get_response(request)
    return middleware


def store_subdomain(get_response):
    """indigo.beauty-grid.com/...  ->  the store at /shop/indigo/... (when STORE_DOMAIN is set)."""
    from django.conf import settings
    skip = ("/shop/", "/i18n/", "/static/", "/media/")

    def middleware(request):
        domain = getattr(settings, "STORE_DOMAIN", "")
        host = request.get_host().split(":")[0].lower()
        if domain and host.endswith("." + domain):
            sub = host[: -len(domain) - 1]
            if sub and "." not in sub and sub not in ("www", "admin", "app"):
                try:
                    sub = sub.encode().decode("idna")  # Arabic store names arrive as xn--...
                except UnicodeError:
                    pass
                if not request.path_info.startswith(skip):
                    request.path_info = f"/shop/{sub}{request.path_info}"
        return get_response(request)
    return middleware
