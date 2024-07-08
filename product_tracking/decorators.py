from django.http import HttpResponse


def check_module_access(module_name):
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            if module_name in request.session.get('modules', []):
                return view_func(request, *args, **kwargs)
            else:
                return HttpResponse('You do not have permission to access this module.', status=403)

        return _wrapped_view

    return decorator
