import contextlib
import enum
import resource
import time
import io

import ws.profile
import ws.auth
import ws.cgi
import ws.http.parser
import ws.http.structs
import ws.http.utils
import ws.ratelimit
import ws.serve
import ws.sockets
from ws.config import config
from ws.err import *
from ws.http.utils import request_is_persistent, response_is_persistent
from ws.logs import error_log, access_log

CLIENT_ERRORS_THRESHOLD = config.getint('http', 'client_errors_threshold')


class ConnectionWorker:
    def __init__(self, sock, address, *, auth_scheme, static_files,
                 worker_stats, persist_connection):
        self.sock = sock
        self.address = address
        self.finished = False
        self.persist_connection = persist_connection
        self.auth_scheme = auth_scheme
        self.static_files = static_files
        self.worker_stats = worker_stats
        self.exchange = None
        self.push_exchange()

    def push_exchange(self):
        self.exchange = HTTPExchange(
            sock=self.sock, address=self.address, auth_scheme=self.auth_scheme,
            static_files=self.static_files, worker_stats=self.worker_stats,
            persist_connection=self.persist_connection
        )

    def work(self, readable_fds, writable_fds, failed_fds):
        if self.exchange.state == HTTPExchange.States.finished:
            if self.persist_connection and self.exchange.persisted_connection():
                self.push_exchange()
            else:
                self.finished = True
                return
        else:
            assert isinstance(self.exchange, HTTPExchange)
            self.exchange.process(readable_fds=readable_fds,
                                  writable_fds=writable_fds,
                                  failed_fds=failed_fds)


class HTTPExchange:
    class States(enum.Enum):
        parsing_request = 'parsing_request'
        handling_request = 'handling_request'
        reading_response = 'reading_response'
        sending_response = 'sending_response'
        cleaning = 'cleaning'
        finished = 'finished'
        # impossible to send a reply
        broken = 'broken'

    def __init__(self, sock, address, *, auth_scheme, static_files,
                 worker_stats, persist_connection):
        self.sock = sock
        self.address = address
        self.required_fds = [self.sock]
        self.persist_connection = persist_connection
        self.state = self.States.parsing
        self.state_machine = {
            self.States.parsing: self.parse_request,
            self.States.handling_request: self.handle_request,
            self.States.reading_response: self.read_response,
            self.States.sending_response: self.send_response,
            self.States.cleaning: self.cleanup,
            self.States.broken: self.handle_broken
        }
        self.error = None
        self.request_profile = ws.profile.Timer()
        self.request_receiver = ws.http.parser.RequestReceiver(sock=sock)
        self.request_handler = RequestHandler(
            sock=sock, address=address, persist_connection=persist_connection,
            auth_scheme=auth_scheme, static_files=static_files,
            worker_stats=worker_stats
        )
        self.response_reader = ResponseReader(sock=sock)
        self.response_sender = ResponseSender(sock=sock)

    def process(self, readable_fds, writable_fds, failed_fds):
        assert self.state != self.States.finished

        while self.state != self.States.finished:
            method = self.state_machine[self.state]
            # noinspection PyBroadException
            try:
                if self.sock.fileno() in failed_fds:
                    msg = 'Client through socket {} dropped the TCP connection.'
                    raise BrokenSocketException(msg=msg.format(self.sock),
                                                code='BROKEN_CLIENT_SOCKET')

                with self.request_profile.time(self.state.value):
                    next_state = method(readable_fds=readable_fds,
                                        writable_fds=writable_fds,
                                        failed_fds=failed_fds)
            except StateWouldBlockException as err:
                error_log.debug3("State %s can't complete due to blocking IO. "
                                 "Error=%s", err)
                break
            except (SignalReceivedException, KeyboardInterrupt):
                # TODO this drops the client connection without a response.
                # is this okay ?
                raise
            except BaseException as exc_val:
                if self.state in (self.request_receiver, self.request_handler):
                    error_log.exception('Unhandled exception while handling '
                                        'request. HTTPExchange state is %s. '
                                        'Sending a 500 to client.',
                                        self.state)
                    response = ws.http.utils.build_response(500)
                    self.response_reader.read_from(response)
                    self.state = self.States.reading_response
                else:
                    error_log.exception("HTTP exchange through socket %s "
                                        "entered a broken state.",
                                        self.sock)
                    self.state = self.States.broken
                    # TODO this might not be ok
                    self.error = exc_val
            else:
                self.state = next_state

    def parse_request(self, readable_fds, writable_fds, failed_fds):
        assert self.state == self.States.parsing_request
        assert not self.request_receiver.is_finished()

        if self.sock.fileno() not in readable_fds:
            raise StateWouldBlockException(msg='Cannot read from socket {}.'
                                           .format(self.sock),
                                           code='PARSE_REQ_SOCK_NOT_READABLE')
        try:
            while not self.request_receiver.is_finished():
                self.request_receiver.do_recv()
        except ws.http.parser.ParserException:
            error_log.exception('Socket %s failed.')
            self.response_reader.read_from(ws.http.utils.build_response(400))
            return self.States.reading_response
        except BlockingIOError as err:
            msg = 'No more data in read buffer of socket {}.'
            raise StateWouldBlockException(msg=msg.format(self.sock),
                                           code='PARSE_REQ_SOCK_READ_EXH') from err

        lines, leftover_body = self.request_receiver.split_lines()
        try:
            request = ws.http.parser.parse(lines=lines)
        except ws.http.parser.ParserException:
            self.response_reader.read_from(ws.http.utils.build_response(400))
            return self.States.reading_response
        else:
            self.request_handler.handle_request(request=request,
                                                leftover_body=leftover_body)
            return self.States.handling_request

    def handle_request(self, readable_fds, writable_fds, failed_fds):
        assert self.state == self.States.handling_request
        response = self.request_handler.process()
        assert self.request_handler.finished
        self.response_reader.read_from(response=response)
        return self.States.reading_response

    def read_response(self, readable_fds, writable_fds, failed_fds):
        assert self.state == self.States.reading_response
        # TODO check if fds of response are ready
        if self.response_reader.finished:
            return self.States.finalizing
        else:
            chunk = self.response_reader.read()
            self.response_sender.stream_chunk(chunk)
            return self.States.sending_response

    def send_response(self, readable_fds, writable_fds, failed_fds):
        assert self.state == self.States.sending_response
        self.response_sender.send_chunk()
        if self.response_sender.finished:
            return self.States.reading_response
        else:
            return self.States.sending_response

    def cleanup(self, readable_fds, writable_fds, failed_fds):
        assert self.state == self.States.cleaning
        self.response_reader.close()
        access_log.log(request=self.request_handler.request,
                       response=self.request_handler.response)
        return self.States.finished

    def handle_broken(self, readable_fds, writable_fds, failed_fds):
        return self.States.cleaning

    def persisted_connection(self):
        return self.request_handler.persisted_connection()


class RequestHandler:
    def __init__(self, sock, address, *,
                 auth_scheme, worker_stats, static_files, persist_connection):
        assert isinstance(sock, ws.sockets.Socket)
        self.sock = sock
        self.address = address
        self.request = None
        self.response = None
        self.leftover_body = None
        self.persist_connection = persist_connection
        self.auth_scheme = auth_scheme
        self.worker_stats = worker_stats
        self.static_files = static_files
        self.finished = False

    def handle_request(self, request, leftover_body):
        assert isinstance(request, ws.http.structs.HTTPRequest)

        self.request = request
        self.leftover_body = leftover_body

    def process(self):
        auth_check = self.auth_scheme.check(request=self.request,
                                            address=self.address)
        is_authorized, auth_response = auth_check

        route = self.request.request_line.request_target.path
        method = self.request.request_line.method
        error_log.debug3('Incoming request {} {}'.format(method, route))

        if not is_authorized:
            response = auth_response
        elif method == 'GET':
            if ws.serve.is_status_route(route):
                response = ws.serve.worker_status(self.worker_stats)
            else:
                static_response = self.static_files.get_route(route)
                if static_response.status_line.status_code == 200:
                    response = static_response
                elif ws.cgi.can_handle_request(self.request):
                    response = ws.cgi.execute_script(
                        self.request,
                        socket=self.sock,
                        body_start=self.leftover_body
                    )
                else:
                    response = ws.http.utils.build_response(404)
        elif ws.cgi.can_handle_request(self.request):
            response = ws.cgi.execute_script(
                self.request,
                socket=self.sock,
                body_start=self.leftover_body
            )
        else:
            response = ws.http.utils.build_response(405)

        if not self.persist_connection:
            error_log.debug('Closing connection. (explicitly)')
            conn = 'close'
        else:
            try:
                conn = str(self.request.headers['Connection'],
                           encoding='ascii')
            except (KeyError, UnicodeDecodeError):
                error_log.debug(
                    'Getting Connection header from request '
                    'failed. Closing connection.')
                conn = 'close'
        response.headers['Connection'] = conn
        self.finished = True
        self.response = response
        return response

    def persisted_connection(self):
        client_persists = self.request and request_is_persistent(self.request)
        server_persists = (self.response and
                           response_is_persistent(self.response))

        return client_persists and server_persists


class ResponseReader:
    def __init__(self, sock):
        assert isinstance(sock, ws.sockets.Socket)
        self.sock = sock
        self.response = None
        self.chunks = None
        self.current_chunk = None
        self.finished = False

    def read_from(self, response):
        assert isinstance(response, ws.http.structs.HTTPResponse)
        self.response = response
        self.chunks = self.response_iterator()

    def read(self):
        assert not self.finished

        if not self.current_chunk:
            try:
                self.current_chunk = next(self.chunks)
            except StopIteration:
                self.current_chunk = b''
                self.finished = True

        return self.current_chunk

    def close(self):
        error_log.critical(
            'Close() method on Response reader is not implemented.')
        # TODO
        return

    def response_iterator(self, chunk_size=4096):
        assert isinstance(chunk_size, int)
        http_fields = io.BytesIO()
        http_fields.write(bytes(self.response.status_line))
        http_fields.write(b'\r\n')
        http_fields.write(bytes(self.response.headers))
        http_fields.write(b'\r\n\r\n')

        http_fields.seek(0)
        chunk = http_fields.read(chunk_size)
        while chunk:
            yield chunk
            chunk = http_fields.read(chunk_size)

        yield from self.response.body


class ResponseSender:
    def __init__(self, sock):
        assert isinstance(sock, ws.sockets.Socket)
        self.sock = sock
        self.chunk_to_send = None
        self.finished = False
        self.sent = 0

    def stream_chunk(self, chunk):
        assert isinstance(chunk, (bytearray, bytes))
        self.chunk_to_send = chunk

    def send_chunk(self):
        assert not self.finished

        self.sent += self.sock.send(self.chunk_to_send)
        self.chunk_to_send = self.chunk_to_send[self.sent:]
        self.finished = bool(self.chunk_to_send)


@contextlib.contextmanager
def record_rusage(dct):
    dct['request_start'] = time.time()
    rusage_start = resource.getrusage(resource.RUSAGE_SELF)
    start_key = 'ru_{}_start'
    end_key = 'ru_{}_end'
    ru_key = 'ru_{}'
    for keyword in ('utime', 'stime', 'maxrss'):
        val = getattr(rusage_start, ru_key.format(keyword))
        dct[start_key.format(keyword)] = val
    try:
        yield
    finally:
        rusage_end = resource.getrusage(resource.RUSAGE_SELF)
        for keyword in ('utime', 'stime', 'maxrss'):
            key = end_key.format(keyword)
            val = getattr(rusage_end, ru_key.format(keyword))
            dct[key] = val
        dct['request_end'] = time.time()
        dct['request_time'] = dct['request_end'] - dct['request_start']

        for keyword in ('ru_utime', 'ru_stime'):
            dct[keyword] = dct[keyword + '_end'] - dct[keyword + '_start']
