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


class SocketIterator:
    """ Optimal byte iterator over plain sockets.

    The __next__ method of this class ALWAYS returns one byte from the
    underlying socket or raises an exception.

    Exceptions raised during iteration:

    ClientSocketException(code='CS_PEER_SEND_IS_TOO_SLOW') - when __next__ is
        called and the socket times out.
    ClientSocketException(code='CS_PEER_NOT_SENDING' - when __next__ is called
        and the client sends 0 bytes through the socket indicating he is done.
    ClientSocketException(code='CS_CONNECTION_TIMED_OUT') - when __next__ is
        called but the connection_timeout has been exceeded.
    StopIteration() - if __next__ is called after the socket was broken

    """
    buffer_size = 2048
    default_socket_timeout = config.getint('http', 'request_timeout')
    default_connection_timeout = config.getint('http', 'connection_timeout')

    def __init__(self, sock, *,
                 socket_timeout=default_socket_timeout,
                 connection_timeout=default_connection_timeout):
        assert isinstance(sock, (ws.sockets.Socket, ws.sockets.SSLSocket))
        assert isinstance(socket_timeout, int)
        assert isinstance(connection_timeout, int)

        self.sock = sock
        self.current_chunk = None
        self.socket_broke = False
        self.connection_timeout = connection_timeout
        self.connected_on = time.time()
        self.written = False

        self.sock.settimeout(socket_timeout)

    def __iter__(self):
        return self

    def __next__(self):
        if self.current_chunk:
            try:
                return next(self.current_chunk)
            except StopIteration:
                pass
        elif self.socket_broke:
            raise StopIteration()

        if self.connected_on + self.connection_timeout < time.time():
            raise ClientSocketException(code='CS_CONNECTION_TIMED_OUT')

        try:
            chunk = self.sock.recv(self.__class__.buffer_size)
        except ws.sockets.TimeoutException as e:
            error_log.warning('Socket timed out while receiving request.')
            raise ClientSocketException(code='CS_PEER_SEND_IS_TOO_SLOW') from e

        error_log.debug3('Read chunk %s', chunk)

        if chunk == b'':
            error_log.info('Socket %d broke', self.sock.fileno())
            self.socket_broke = True
            raise ClientSocketException(code='CS_PEER_NOT_SENDING')

        self.current_chunk = iter(chunk)

        return next(self.current_chunk)


def recv_request(sock,
                 chunk_size=4096,
                 timeout=config.getint('http', 'connection_timeout'),
                 connection_timeout=config.getint('http', 'request_timeout')):
    error_log.debug3('recv_request() from %s', sock)
    assert isinstance(sock, ws.sockets.Socket)
    assert isinstance(chunk_size, int)

    total_length = 0
    chunks = []
    body_offset = -1
    start = time.time()
    sock.settimeout(timeout)
    while body_offset == -1:
        if total_length > MAX_HTTP_META_LEN:
            raise ParserException(code='PARSER_REQUEST_TOO_LONG')
        elif time.time() - start > connection_timeout:
            raise ParserException(code='CS_PEER_CONNECTION_TIMED_OUT')

        try:
            chunk = sock.recv(chunk_size)
        except ws.sockets.TimeoutException:
            raise ParserException(code='CS_PEER_SEND_IS_TOO_SLOW')
        if not chunk:
            raise ParserException(code='CS_PEER_NOT_SENDING')
        chunks.append(chunk)
        total_length += len(chunk)
        body_offset = chunk.find(b'\r\n\r\n')

    lines = []
    leftover_body = b''

    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            line_chunk = chunk[:body_offset]
            leftover_body = chunk[body_offset:]
        else:
            line_chunk = chunk
        lines.extend(line_chunk.split(b'\r\n'))

    return lines, leftover_body


def parse(sock):
    lines, leftover_body = recv_request(sock)

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
    return request, leftover_body


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

