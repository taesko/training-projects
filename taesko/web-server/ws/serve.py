import logging
import os
import socket

import ws.err_responses
from ws.config import config
from ws.err import *
from ws.http.structs import HTTPResponse, HTTPStatusLine, HTTPHeaders

error_log = logging.getLogger('error')
STATIC_ROUTE = config['routes']['static']
STATIC_DIR = os.path.abspath(config['resources']['static_dir'])

assert_sys(STATIC_ROUTE.endswith('/'),
           msg="routes.static must end with a '/'",
           code='CONFIG_BAD_STATIC_ROUTE')
assert_sys(os.path.isdir(STATIC_DIR),
           msg='resources.static_dir field must be a directory',
           code='CONFIG_BAD_STATIC_DIR')


def serve_file_depreciated(sock, route):
    assert isinstance(sock, socket.socket)

    not_found_response = HTTPResponse(
        status_line=HTTPStatusLine(http_version='HTTP/1.1',
                                   status_code=404,
                                   reason_phrase=''),
        headers=HTTPHeaders({'Content-Encoding': 'ascii'}),
        body=None
    )

    if not route.startswith(STATIC_ROUTE):
        not_found_response.send(sock)
        return

    rel_path = route[len(STATIC_ROUTE):]
    abs_path = os.path.join(STATIC_DIR, rel_path)

    try:
        with open(abs_path, mode='r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        not_found_response.send(sock)
        return

    response = HTTPResponse(
        status_line=HTTPStatusLine(http_version='HTTP/1.1',
                                   status_code=200,
                                   reason_phrase=''),
        headers=HTTPHeaders({
            'Content-Length': len(content),
            'Content-Encoding': 'ascii'
        }),
        body=content
    )

    response.send(sock)
    return


def get_file(route):
    if not route.startswith(STATIC_ROUTE):
        return ws.err_responses.not_found()

    rel_path = route[len(STATIC_ROUTE):]
    file_path = os.path.join(STATIC_DIR, rel_path)
    resolved = os.path.abspath(os.path.realpath(file_path))
    # if a symlink get's created after this if does the check an exploit is
    # possible
    if not resolved.startswith(STATIC_DIR):
        return ws.err_responses.not_found()

    try:
        with open(resolved, mode='r', encoding='utf-8') as f:
            content = f.read()
    except (FileNotFoundError, IsADirectoryError):
        return ws.err_responses.not_found()

    return HTTPResponse(
        status_line=HTTPStatusLine(http_version='HTTP/1.1',
                                   status_code=200,
                                   reason_phrase=''),
        headers=HTTPHeaders({
            'Content-Length': len(content),
            'Content-Encoding': 'ascii'
        }),
        body=content
    )


def serve_response_depreciated(sock, response):
    error_log.debug('Sending back response %s', response)
    return sock.sendall(bytes(response))
