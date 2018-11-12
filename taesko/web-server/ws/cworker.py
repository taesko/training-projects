import contextlib
import enum
import io
import resource
import time

import ws.auth
import ws.cgi
import ws.http.parser
import ws.http.structs
import ws.http.utils
import ws.profile
import ws.ratelimit
import ws.serve
import ws.sockets
from ws.config import config
from ws.http.utils import request_is_persistent, response_is_persistent
from ws.logs import error_log, access_log
from ws.utils import StateMachine, StateWouldBlockException
from ws.err import BrokenSocketException

CLIENT_ERRORS_THRESHOLD = config.getint('http', 'client_errors_threshold')


class ConnectionWorker:
    def __init__(self, sock, address, *, auth_scheme, static_files,
                 worker_stats, persist_connection):
        self.sock = sock
        self.address = address
        self.persist_connection = persist_connection
        self.auth_scheme = auth_scheme
        self.static_files = static_files
        self.worker_stats = worker_stats
        self.exchanges = []

    @property
    def finished(self):
        if not self.exchanges:
            return False
        else:
            e = self.exchanges[-1]
            return e.state_machine.finished() and not e.persisted_connection()

    def status_codes(self):
        codes = []

        error_log.debug3('HTTPExchanges are %s', self.exchanges)
        for exchange in self.exchanges:
            code = exchange.response.status_line.status_code
            codes.append(code)

        return codes

    def make_new_exchange(self):
        return HTTPExchange(
            sock=self.sock, address=self.address, auth_scheme=self.auth_scheme,
            static_files=self.static_files, worker_stats=self.worker_stats,
            persist_connection=self.persist_connection
        )

    def process(self, readable_fds, writable_fds, failed_fds):
        if not self.exchanges:
            self.exchanges.append(self.make_new_exchange())

        finished = self.exchanges[-1].state_machine.finished()
        persisted = self.exchanges[-1].persisted_connection()
        if finished:
            if persisted:
                error_log.debug('HTTP connection is persisted. Beginning a '
                                'new exchange with client on %s / %s',
                                self.sock, self.address)
                self.exchanges.append(self.make_new_exchange())
            else:
                assert False

        self.exchanges[-1].state_machine.run(readable_fds=readable_fds,
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
        self.state_machine = StateMachine(
            states={
                'parsing_request': ('handling_request', 'reading_response',
                                    'parsing_request'),
                'handling_request': ('reading_response',),
                'reading_response': ('sending_response', 'cleaning_up'),
                'sending_response': ('sending_response', 'reading_response'),
                'cleaning_up': ('done',),
                'done': (),
                'failed': ('sending_response', )
            },
            callbacks={
                'parsing_request': self.parse_request,
                'handling_request': self.handle_request,
                'reading_response': self.read_response,
                'sending_response': self.send_response,
                'cleaning_up': self.cleanup,
                'failed': self.build_error_response,
            },
            initial_state='parsing_request',
            state_on_exc='failed',
            allowed_exc_handling={'parsing_request', 'handling_request'}
        )
        self.sock = sock
        self.address = address

        self.request_receiver = ws.http.parser.RequestReceiver(sock=sock)
        self.request = None
        self.leftover_body = None

        self.response = None

        self.response_chunks_iter = None
        self.response_chunk = None

        self.persist_connection = persist_connection
        self.auth_scheme = auth_scheme
        self.static_files = static_files
        self.worker_stats = worker_stats

        self.read_fds = []
        self.write_fds = []

    def run(self, readable_fds, writable_fds, failed_fds):
        if self.sock.fileno() in failed_fds:
            exc = BrokenSocketException(msg='Connection %s / %s broke.'
                                        .format(self.sock, self.address),
                                        code='EXCHANGE_SOCKET_FD_FAILED')
            self.state_machine.throw(exc)
            self.state_machine.run()
        all_fds = (*self.read_fds, *self.write_fds)
        failed = any(fd in failed_fds for fd in all_fds)
        can_read = all(fd in readable_fds for fd in self.read_fds)
        can_write = all(fd in writable_fds for fd in self.write_fds)
        elif self.can_do_io(readable_fds, writable_fds, failed_fds):
            self.state_machine.run()

    def fail_on_dropped_socket(self, method):
        def wrapped(*args, **kwargs):
            if
    def persisted_connection(self):
        client_persists = self.request and request_is_persistent(self.request)
        server_persists = (self.response and
                           response_is_persistent(self.response))

        return client_persists and server_persists

    def build_error_response(self):
        error_log.error('Exception occurred during processing of request '
                        'on connection %s / %s. Sending a 500 to client.',
                        self.sock, self.address,
                        exc_info=self.state_machine.exception)
        self.response = ws.http.utils.build_response(500)

    def parse_request(self, readable_fds, writable_fds, failed_fds):
        if self.sock.fileno() not in readable_fds:
            raise StateWouldBlockException(msg='Cannot read from socket {}.'
                                           .format(self.sock),
                                           code='PARSE_REQ_SOCK_BLOCKS')
        try:
            while not self.request_receiver.is_finished():
                self.request_receiver.do_recv()
        except ws.http.parser.ParserException:
            error_log.exception('Socket %s failed.')
            self.response = ws.http.utils.build_response(400)
            self.state_machine.transition_to('reading_response')
            return
        except BlockingIOError as err:
            msg = 'No more data in read buffer of socket {}.'
            exc = StateWouldBlockException(msg=msg.format(self.sock),
                                           code='PARSE_REQ_SOCK_READ_EXH')
            raise exc from err

        if not self.request_receiver.is_finished():
            self.state_machine.transition_to('parsing_request')
            return

        lines, leftover_body = self.request_receiver.split_lines()
        error_log.debug3('Received lines %s with leftover body: %s.',
                         lines,
                         leftover_body)
        try:
            self.request = ws.http.parser.parse(lines=lines)
            self.leftover_body = leftover_body
        except ws.http.parser.ParserException as err:
            error_log.warning('Parsing error occurred with CODE=%s '
                              'and MSG=%s.', err.code, err.msg)
            self.response = ws.http.utils.build_response(400)
            self.state_machine.transition_to('reading_response')

    def handle_request(self, readable_fds, writable_fds, failed_fds):
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
        self.response = response

    def read_response(self, readable_fds, writable_fds, failed_fds):
        if not self.response_chunks_iter:
            assert isinstance(self.response, ws.http.structs.HTTPResponse)
            self.response_chunks_iter = response_iterator(self.response)
        try:
            self.response_chunk = next(self.response_chunks_iter)
            error_log.debug3('Read chunk %s', self.response_chunk)
        except StopIteration:
            self.state_machine.transition_to('cleaning_up')

    def send_response(self, readable_fds, writable_fds, failed_fds):
        assert isinstance(self.response_chunk, (bytearray, bytes))
        sent = self.sock.send(self.response_chunk)
        error_log.debug3('Sent %s / %s total bytes of current chunk.',
                         sent, len(self.response_chunk))
        self.response_chunk = self.response_chunk[sent:]
        if not self.response_chunk:
            self.state_machine.transition_to('reading_response')

    # noinspection PyUnusedLocal
    def cleanup(self, readable_fds, writable_fds, failed_fds):
        access_log.log(request=self.request,
                       response=self.response)


def response_iterator(response, chunk_size=4096):
    assert isinstance(response, ws.http.structs.HTTPResponse)
    assert isinstance(chunk_size, int)
    http_fields = io.BytesIO()
    http_fields.write(bytes(response.status_line))
    http_fields.write(b'\r\n')
    http_fields.write(bytes(response.headers))
    http_fields.write(b'\r\n\r\n')

    http_fields.seek(0)
    chunk = http_fields.read(chunk_size)
    while chunk:
        yield chunk
        chunk = http_fields.read(chunk_size)

    yield from response.body


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
