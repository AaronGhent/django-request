from django.core.urlresolvers import get_callable

from request.models import Request
from request import settings
from request.router import patterns


class RequestMiddleware(object):
    def __init__(self):
        self.exceptions = patterns(False, *settings.REQUEST_IGNORE_PATHS)

    def process_response(self, request, response):
        if request.method.lower() not in settings.REQUEST_VALID_METHOD_NAMES:
            return response

        if response.status_code < 400 and settings.REQUEST_ONLY_ERRORS:
            return response

        if self.exceptions.resolve(request.path):
            return response

        if request.is_ajax() and settings.REQUEST_IGNORE_AJAX:
            return response

        if request.META.get('REMOTE_ADDR') in settings.REQUEST_IGNORE_IP:
            return response

        if getattr(request, 'user', False):
            if request.user.username in settings.REQUEST_IGNORE_USERNAME:
                return response

        Request.objects.create_from_http_request(request, response)

        return response
