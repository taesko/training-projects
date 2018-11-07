import re
import time

import ws.http.structs
import ws.utils
import ws.sockets
from ws.config import config
from ws.err import *
from ws.logs import error_log

MAX_HTTP_META_LEN = config.getint('http', 'max_http_meta_len')
ALLOWED_HTTP_METHODS = frozenset({
    'HEAD',
    'GET',
    'POST',
    'PUT',
    'DELETE',
    'CONNECT',
    'OPTIONS',
    'TRACE'
})


class ParserException(ServerException):
    default_msg = 'Failed to parse request due to bad syntax.'
    default_code = 'PARSER_BAD_SYNTAX'

    def __init__(self, msg=default_msg, code=default_code):
        super().__init__(msg=msg, code=code)


class ClientSocketException(ParserException):
    default_msg = 'Client socket caused an error.'
    default_code = 'CS_ERROR'

    def __init__(self, msg=default_code, code=default_code):
        super().__init__(msg=msg, code=code)


class RequestReceiver:
    def __init__(
            self,
            sock,
            chunk_size=4096,
            timeout=config.getint('http', 'request_timeout'),
            connection_timeout=config.getint('http', 'connection_timeout')
    ):
        assert isinstance(sock, ws.sockets.Socket)
        assert isinstance(chunk_size, int)
        assert isinstance(timeout, int)
        assert isinstance(connection_timeout, int)

        self.sock = sock
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.connection_timeout = connection_timeout
        self.start = time.time()
        self.body_offset = -1
        self.total_length = 0
        self.chunks = []
        self.sock.settimeout(self.timeout)

    def is_finished(self):
        return self.body_offset != -1

    def do_recv(self):
        assert not self.is_finished()

        if self.total_length > MAX_HTTP_META_LEN:
            raise ParserException(code='PARSER_REQUEST_TOO_LONG')
        elif time.time() - self.start > self.connection_timeout:
            raise ParserException(code='CS_PEER_CONNECTION_TIMED_OUT')

        try:
            chunk = self.sock.recv(self.chunk_size)
        except ws.sockets.TimeoutException:
            raise ParserException(code='CS_PEER_SEND_IS_TOO_SLOW')
        if not chunk:
            raise ParserException(code='CS_PEER_NOT_SENDING')
        self.total_length += len(chunk)
        self.body_offset = chunk.find(b'\r\n\r\n')
        if self.body_offset != -1:
            self.body_offset += 4

    def split_lines(self):
        assert self.is_finished()
        lines = []
        leftover_body = b''
        for i, chunk in enumerate(self.chunks):
            if i == len(self.chunks) - 1:
                line_chunk = chunk[:self.body_offset]
                leftover_body = chunk[self.body_offset:]
            else:
                line_chunk = chunk
            lines.extend(line_chunk.split('\r\n'))
        return lines, leftover_body


def parse(lines):
    try:
        request_line = parse_request_line(lines[0])
        error_log.debug2('Parsed request line %s', request_line)
        headers = parse_headers(lines[1:])
    except UnicodeDecodeError as err:
        raise ParserException(code='BAD_ENCODING') from err

    error_log.debug2('headers is %r with type %r', headers, type(headers))
    error_log.debug2('Deferring parsing of body to later.')

    request = ws.http.structs.HTTPRequest(request_line=request_line,
                                          headers=headers)
    return request


HTTP_VERSION_REGEX = re.compile(b'HTTP/(\\d\\.\\d)')


def parse_request_line(line, *, methods=ALLOWED_HTTP_METHODS):
    error_log.debug3('Parsing request line...')
    parts = line.split(b' ')

    if len(parts) != 3:
        raise ParserException(code='PARSER_BAD_REQUEST_LINE')

    method, request_target, http_version = parts
    method = method.decode('ascii')

    if method not in methods:
        raise ParserException(code='PARSER_UNKNOWN_METHOD')

    error_log.debug2('Parsed method %r', method)

    uri = parse_request_target(request_target)
    error_log.debug2('Parsed uri %r', uri)

    if not HTTP_VERSION_REGEX.match(http_version):
        raise ParserException(code='PARSER_BAD_HTTP_VERSION')

    http_version = http_version.decode('ascii')

    return ws.http.structs.HTTPRequestLine(method=method,
                                           request_target=uri,
                                           http_version=http_version)


def parse_request_target(iterable):
    string = bytes(iterable).decode('ascii')
    if string[0] == '/':
        # origin form
        path, query = parse_path(string)
        return ws.http.structs.URI(
            protocol=None,
            host=None,
            port=None,
            path=path,
            query=query,
        )
    elif string.startswith('http://') or string.startswith('https://'):
        # absolute-form
        protocol, *rest = string.split('://')

        if len(rest) != 1:
            raise ParserException(code='PARSER_BAD_ABSOLUTE_FORM_URI')

        parts = rest[0].split('/')

        if len(parts) <= 1:
            raise ParserException(code='PARSER_MISSING_AUTHORITY')

        user_info, host, port = parse_authority(parts[0])

        absolute_path = '/' + '/'.join(parts[1:])
        path, query = parse_path(absolute_path)

        return ws.http.structs.URI(
            protocol=protocol,
            host=host,
            port=port,
            path=path,
            query=query
        )
    elif string == '*':
        # asterisk form
        raise NotImplementedError()
    else:
        # authority form
        user_info, host, port = parse_authority(string)

        return ws.http.structs.URI(
            protocol=None,
            host=host,
            port=port,
            path=None,
            query=None
        )


AUTHORITY_REGEX = re.compile(r'([^@:]*@)?([^@:]+)(:\d*)?')


def parse_authority(authority):
    matched = AUTHORITY_REGEX.match(authority)

    if not matched:
        raise ParserException(code='PARSER_INVALID_AUTHORITY')

    user_info_, host_, port_ = matched.groups()
    user_info_ = user_info_[:-1] if user_info_ else None
    port_ = port_[1:] if port_ else None

    return user_info_, host_, port_


def parse_path(full_path):
    parts_ = full_path.split('?')
    if len(parts_) == 1:
        return parts_[0], None
    else:
        return parts_[0], '?'.join(parts_[1:])


def parse_headers(lines):
    """ Parses HTTP headers from an iterable into a dictionary.

    NOTE: Does not parse indented multi-line headers
    """
    headers = {}
    for line in lines:
        field, _, value = line.partition(b':')
        if not value:
            raise ParserException(code='PARSER_BAD_HEADER')

        field = field.decode('ascii').strip()
        headers[field] = value.lstrip()
        error_log.debug3('Parsed header field %s with value %r', field, value)

    return headers
