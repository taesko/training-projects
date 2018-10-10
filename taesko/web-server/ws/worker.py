import collections
import errno
import signal

import ws.http.parser
import ws.http.structs
import ws.http.utils
import ws.ratelimit
import ws.serve
import ws.sockets
from ws.config import config
from ws.err import *
from ws.logs import error_log, access_log

CLIENT_ERRORS_THRESHOLD = config.getint('http', 'client_errors_threshold')
HTTPExchange = collections.namedtuple('HTTPExchange', ['request', 'response'])


# noinspection PyAttributeOutsideInit
class Worker:
    """ Receives/parses requests and sends/encodes back responses.

    Instances of this class MUST be used through a context manager to ensure
    proper clean up of resources.

    The methods self.parse_request() and self.respond() MUST only be used
    when the property self.http_connection_is_open is True.

    Persistent connections are handled, but require self.parse_request() to be
    repeatedly called.
    """
    max_exchange_history = CLIENT_ERRORS_THRESHOLD * 2

    # noinspection PyUnusedLocal
    def __init__(self, iterable_socket, address):
        assert isinstance(iterable_socket, ws.sockets.ClientSocket)
        self.sock = iterable_socket
        self.responding = False
        self.status_code_on_abort = None
        self.exchanges = collections.deque(maxlen=self.max_exchange_history)

        signal.signal(signal.SIGTERM, self.handle_termination)

    @property
    def http_connection_is_open(self):
        def server_closed_connection(response):
            if not response:
                return False
            elif 'Connection' not in response.headers:
                return False
            else:
                pass

            c = response.headers['Connection']
            hop_by_hop_headers = (h.strip() for h in c.split(','))

            return b'close' in hop_by_hop_headers

        if not self.request or not self.response:
            return True

        return (not server_closed_connection(self.response) and
                ws.http.utils.request_is_persistent(self.request))

    @property
    def request(self):
        try:
            return self.exchanges[-1].request
        except IndexError:
            return None

    @request.setter
    def request(self, http_request):
        try:
            self.exchanges[-1] = HTTPExchange(http_request,
                                              self.exchanges[-1].response)
        except IndexError:
            self.exchanges.append(HTTPExchange(http_request, None))

    @property
    def response(self):
        try:
            return self.exchanges[-1].response
        except IndexError:
            return None

    @response.setter
    def response(self, http_response):
        try:
            self.exchanges[-1] = HTTPExchange(self.exchanges[-1].request,
                                              http_response)
        except IndexError:
            self.exchanges.append(HTTPExchange(None, http_response))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not exc_val:
            error_log.info('Execution successful. Cleaning up worker.')
            self.sock.close(with_shutdown=True, safely=True)
            return False

        error_log.info('Execution failed. Cleaning up worker.')

        if self.responding:
            error_log.warning(
                'An exception occurred after worker had sent bytes over '
                'the socket(fileno=%s). Client will receive an invalid '
                'HTTP response.',
                self.sock.fileno()
            )
            self.sock.close(with_shutdown=True, safely=False)
            return False
        else:
            pass

        response = handle_err(exc_val)

        if not response:
            error_log.exception('Could not handle exception. Client will '
                                'receive a 500 Internal Server Error.')
            response = ws.http.utils.build_response(500)

        response.headers['Connection'] = 'close'

        self.status_code_on_abort = response.status_line.status_code

        ignored_request = isinstance(exc_val, (ws.sockets.ClientSocketError,
                                               ws.http.parser.ParserError))

        try:
            self.respond(response, ignored_request=ignored_request)
        except OSError as e:
            error_log.warning('During cleanup of worker tried to respond to '
                              'client and close the connection but: '
                              'caught OSError with errno %d.', e.errno)

            if e.errno == errno.ECONNRESET:
                error_log.warning('Client stopped listening prematurely.'
                                  ' (no Connection: close header was received)')
                return True
            else:
                raise
        finally:
            self.sock.close(with_shutdown=True, safely=True)

        return True

    def work(self, request_handler):
        """ Continually serve an http connection.

        :param request_handler: Per-request handler. Will be called with a
            request object as a single argument. The return value must be a
            response object that will be sent to the client.
        :return: This method doesn't return until the connection is closed.
        """
        while self.http_connection_is_open:
            error_log.info('HTTP Connection is open. Parsing request...')
            # TODO client's connection might drop while recv()
            # no need to send him a response
            self.exchanges.append(HTTPExchange(None, None))
            self.request = ws.http.parser.parse(self.sock)
            self.response = request_handler(self.request)
            self.respond(self.response)

    def respond(self, response, *, closing=False, ignored_request=False):
        """

        :param response: Response object to send to client.
        :param closing: Boolean switch whether the http connection should be
            closed after this response.
        :param ignored_request: Boolean flag whether the request sent from the
            client was not read through the socket. (this will imply that an
            empty request string ("") will be sent to the access.log)
        :return:
        """
        assert isinstance(response, ws.http.structs.HTTPResponse)
        assert isinstance(closing, bool)
        assert isinstance(ignored_request, bool)
        assert self.http_connection_is_open

        # TODO there needs to be a way to send Close connection through here.
        # instead of timing out and getting terminated.
        if closing:
            response.headers['Connection'] = 'close'
        elif not ignored_request:
            c_headers = str(self.request.headers.get('Connection', b''),
                            encoding='ascii')
            response.headers['Connection'] = c_headers

        self.responding = True
        self.response = response
        self.sock.send_all(bytes(response))
        self.responding = False

        if ignored_request:
            access_log.log(request=None, response=response)
        else:
            access_log.log(request=self.request,
                           response=response)

    # noinspection PyUnusedLocal
    def handle_termination(self, signum, stack_info):
        assert signum == signal.SIGTERM

        error_log.info('Parent process requested termination.'
                       ' Cleaning up as much as possible and'
                       ' and sending a service unavailable response')

        # No salvation if bytes have already been sent over the socket
        assert not self.responding

        response = ws.http.utils.build_response(503)
        response.headers['Connection'] = 'close'
        self.sock.send_all(bytes(response))

        # TODO what kind of error is this ?
        # raise PeerError(msg='Parent process requested termination.',
        #                 code='PROCESSING_TIMED_OUT')
        raise RuntimeError('Parent process requested termination.')


# noinspection PyUnusedLocal
@err_handler(AssertionError)
def server_err_handler(exc):
    error_log.exception('Internal server error.')
    return ws.http.utils.build_response(500)


@err_handler(ws.http.parser.ParserError)
def handle_parse_err(exc):
    error_log.info('Parsing error with code=%s occurred', exc.code)
    return ws.http.utils.build_response(400)


@err_handler(ws.sockets.ClientSocketError)
def handle_client_socket_err(exc):
    error_log.info('Client socket error with code=%s occurred', exc.code)
    if exc.code in ('CS_PEER_SEND_IS_TOO_SLOW', 'CS_CONNECTION_TIMED_OUT'):
        return ws.http.utils.build_response(408)
    else:
        return ws.http.utils.build_response(400)


def work(client_socket, address, quick_reply_with=None):
    assert isinstance(client_socket, ws.sockets.ClientSocket)
    assert isinstance(address, collections.Container)
    assert (not quick_reply_with or
            isinstance(quick_reply_with, ws.http.structs.HTTPResponse))

    # noinspection PyBroadException
    try:
        with Worker(client_socket, address) as worker:
            if quick_reply_with:
                worker.respond(quick_reply_with, closing=True,
                               ignored_request=True)
                return 0

            worker.work(request_handler=handle_request)
    except Exception:
        error_log.exception('Worker on socket %s and address %s finished'
                            ' abruptly.', client_socket.fileno(), address)
        return 1

    codes = (e.response.status_line.status_code
             for e in worker.exchanges if e.response)
    err_count = sum(c in ws.ratelimit.CONSIDERED_CLIENT_ERRORS for c in codes)

    if err_count:
        return ws.ratelimit.RATE_LIMIT_EXIT_CODE_OFFSET + err_count
    else:
        return 0


def handle_request(request):
    route = request.request_line.request_target.path

    if request.request_line.method == 'GET':
        return ws.serve.get_file(route)
    elif request.request_line.method == 'POST':
        encoding = request.headers.get('Content-Encoding', 'utf-8')
        body = request.body
        return ws.serve.upload_file(route=route, body_stream=body,
                                    encoding=encoding)
    elif request.request_line.method == 'DELETE':
        return ws.serve.delete_file(route)
    else:
        return ws.http.utils.build_response(405)
