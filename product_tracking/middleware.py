from django.shortcuts import redirect
from django.urls import reverse


class SessionAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Check if the user is logged in by looking for 'username' in the session
        if not request.session.get('username') and request.path != reverse('product_tracking:login_view'):
            return redirect(reverse('product_tracking:login_view'))

        response = self.get_response(request)
        return response

