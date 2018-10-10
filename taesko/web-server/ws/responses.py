from copy import deepcopy

import ws.http.parser
from ws.config import config
from ws.err import *
from ws.http.structs import HTTPResponse, HTTPStatusLine, HTTPHeaders
from ws.logs import error_log

error_log.warning('This module is depreciated. See ws.http.utils for building '
                  'of responses and ws.err for error handling.')


def build_response(status_code, *, body=None, reason_phrase='', headers=None,
                   version='HTTP/1.1'):
    status_line = HTTPStatusLine(
        http_version=version,
        status_code=status_code,
        reason_phrase=reason_phrase
    )
    headers = HTTPHeaders(headers or {})

    if body:
        assert 'Content-Encoding' in headers

    return HTTPResponse(
        status_line=status_line,
        headers=headers,
        body=body
    )


ok = build_response(200)
created = build_response(201)
see_other = build_response(303)

bad_request = build_response(400)
forbidden = build_response(403)
not_found = build_response(404)
method_not_allowed = build_response(405)
request_timeout = build_response(408)
length_required = build_response(411)
payload_too_large = build_response(413)
uri_too_long = build_response(414)

internal_server_error = build_response(500)
service_unavailable = HTTPResponse(
    status_line=HTTPStatusLine(
        http_version='HTTP/1.1',
        status_code=503,
        reason_phrase='Service is temporarily down due to overhead.'
    ),
    headers=HTTPHeaders(
        {
            'Retry-After': config.getint('settings', 'process_timeout') * 2
        }
    ),
    body=None,
)

error_responses = {
    'client': {
        PeerError: {
            'CS_PEER_NOT_SENDING': bad_request,
            'CS_PEER_SEND_IS_TOO_SLOW': request_timeout,
            'CS_CONNECTION_TIMED_OUT': request_timeout
        },
        ws.http.parser.ParserError: {
            None: bad_request
        }
    },
    'server': {
        AssertionError: {
            None: internal_server_error
        }
    }
}


def get_err_response(dct, exc_val):
    assert isinstance(exc_val, Exception)

    handlers = None

    for cls in dct:
        if isinstance(exc_val, cls):
            handlers = dct[cls]

    if not handlers:
        return None

    code = getattr(exc_val, 'code', None)

    default_handler = handlers.get(None, None)

    return handlers.get(code, None) or default_handler


def client_err_response_depreciated(exc_val):
    error_log.warning('This function is depreciated see ws.err module...')
    return deepcopy(get_err_response(error_responses['client'], exc_val))


def server_err_response_depreciated(exc_val):
    error_log.warning('This function is depreciated see ws.err module...')
    return deepcopy((get_err_response(error_responses['server'], exc_val) or
                     internal_server_error))
