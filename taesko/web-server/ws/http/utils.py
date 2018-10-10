import enum

from ws.config import config
from ws.http.structs import HTTPStatusLine, HTTPHeaders, HTTPResponse


class HTTPStatusCodes(enum.Enum):
    bad_request = 400
    forbidden = 403
    not_found = 404
    method_not_allowed = 405
    request_timeout = 408
    length_required = 411
    payload_too_large = 413
    uri_too_long = 414

    internal_server_error = 500
    service_unavailable = 503


def build_response(status_code, *, body=None, reason_phrase='', headers=None,
                   version='HTTP/1.1'):
    status_code = HTTPStatusCodes(status_code)

    status_line = HTTPStatusLine(
        http_version=version,
        status_code=status_code,
        reason_phrase=reason_phrase
    )
    headers = HTTPHeaders(headers or {})

    if body:
        assert 'Content-Encoding' in headers

    if status_code == HTTPStatusCodes.service_unavailable:
        headers['Retry-After'] = config.getint('settings',
                                               'process_timeout') * 2

    return HTTPResponse(
        status_line=status_line,
        headers=headers,
        body=body
    )


def request_is_persistent(request):
    if request.request_line.http_version == 'HTTP/1.0':
        return ('Connection' in request.headers and
                b'Keep-Alive' in request.headers['Connection'])
    elif request.request_line.http_version == 'HTTP/1.1':
        return ('Connection' not in request.headers or
                b'close' not in request.headers['Connection'])
