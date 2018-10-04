import collections
import enum
import errno
import logging
import os
import signal
import socket
import time

import ws.err_responses
import ws.http.parser
import ws.serve
from ws.config import config
from ws.err import *

# TODO logging lib needs to be wrapped.
error_log = logging.getLogger('error')


class Server:
    class ExecutionContext(enum.Enum):
        main = 'main'
        worker = 'worker'

    ActiveWorker = collections.namedtuple('ActiveWorker', ['pid', 'created_on'])

    def __init__(self):
        self.host = config['settings']['host']
        self.port = config.getint('settings', 'port')
        self.process_timeout = config.getint('settings', 'process_timeout')
        self.concurrency = config.getint('settings', 'max_concurrent_requests')
        self.process_count_limit = config.getint('settings',
                                                 'process_count_limit')
        self.execution_context = self.ExecutionContext.main

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.workers = collections.deque()

        signal.signal(signal.SIGALRM, self.terminate_hanged_workers)
        signal.alarm(self.process_timeout)

    def __enter__(self):
        error_log.debug('Binding server on %s:%s', self.host, self.port)
        self.sock.bind((self.host, self.port))

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # don't cleanup workers because their sockets were already closed
        # during the self.fork() call
        if self.execution_context == self.ExecutionContext.worker:
            return False

        if exc_type:
            error_log.exception('Unhandled error while listening.'
                                'Shutting down...')

        error_log.info("Closing server's listening socket")
        self.sock.close()

        if self.workers:
            error_log.info('%d child process found. Waiting completion',
                           len(self.workers))

            active_workers = collections.deque()

            for worker in self.workers:
                has_finished, exit_code = os.waitpid(worker.pid, os.WNOHANG)

                if not has_finished:
                    error_log.info('Terminating worker %d', worker.pid)
                    os.kill(signal.SIGTERM, worker.pid)
                else:
                    error_log.info('Reaped worker %d.', worker.pid)
                    active_workers.append(worker)

            self.workers = active_workers

        return False

    def listen(self):
        error_log.info('Listening...')
        self.sock.listen(self.concurrency)

        while True:
            try:
                client_socket, address = self.sock.accept()
            except OSError as err:
                # kernel specific errors are not handled:
                # ENOSR, ESOCKTNOSUPPORT, EPROTONOSUPPORT, ETIMEDOUT
                # consult man 2 socket

                error_log.warning('accept() raised ERRNO=%s', err.errno)

                assert err.errno not in (errno.EBADF, errno.EFAULT,
                                         errno.EINVAL, errno.ENOTSOCK,
                                         errno.EOPNOTSUPP)

                if err.errno == errno.EPERM:
                    error_log.info('Denied client connection, because firewall '
                                   'blocked the accept() call.')
                elif err.errno in (errno.EPROTO, errno.ECONNABORTED):
                    error_log.info('Protocol or connection error from client.')
                    continue
                elif err.errno in (errno.EMFILE, errno.ENFILE,
                                   errno.ENOBUFS, errno.ENOMEM):
                    error_log.error('Socket memory or file descriptors run '
                                    'out. Server will continue listening. '
                                    'But connections will be refused until '
                                    'resources are free.')
                    continue

                assert False

            error_log.debug('Accepted connection. '
                            'client_socket.fileno=%d and address=%s',
                            client_socket.fileno(), address)
            try:
                forked_pid = self.fork(client_socket)
            except PeerError as err:
                error_log.warning(err)

                # noinspection PyBroadException
                try:
                    response = ws.err_responses.service_unavailable()
                    self.work(client_socket, address,
                              quick_reply_with=response)
                except BaseException:
                    logging.exception('Unhandled exception occurred while '
                                      'responding to client from the main'
                                      'process. Catching and continuing'
                                      'to listen.')
                continue

            if forked_pid == 0:
                status = 0
                # noinspection PyBroadException
                try:
                    self.work(client_socket, address)
                except Exception:
                    status = 1
                    logging.exception('Unhandled exception occurred while'
                                      ' Exiting with status code %s', status)
                # noinspection PyProtectedMember
                os._exit(status)
            else:
                pass

    def fork(self, client_socket):
        """ Forks the process and sets the self.execution_context field.

        Also closes the listening socket in the child process and
        the client socket in the parent process.

        Raises the following exceptions:
            OSError with errno = ENOMEM or EAGAIN - if this exception is raised
                the process isn't forked and no sockets are closed.
        """
        assert self.execution_context == self.ExecutionContext.main
        assert isinstance(client_socket, socket.socket)

        assert_peer(len(self.workers) < self.process_count_limit,
                    msg='Cannot fork because process limit has been reached.',
                    code='FORK_PROCESS_COUNT_LIMIT_REACHED')
        try:
            pid = os.fork()
        except OSError as err:
            error_log.warning('fork() raised ERRNO=%d. Reason: %s',
                              err.errno, err.strerror)

            assert_sys(err.errno != errno.ENOSYS,
                       msg='Server cannot continue running without fork '
                           'support. Shutting down.',
                       code='FORK_NO_SUPPORT',
                       from_=err)

            if err.errno in (errno.ENOMEM, errno.EAGAIN):
                error_log.warning('Not enough resources to serve the '
                                  'client connection. Server will continue '
                                  'listening but connections will receive '
                                  '503 until resources are free.')
                raise PeerError(msg='Not enough resources to serve client '
                                    'request through fork.',
                                code='FORK_NOT_ENOUGH_RESOURCES') from err

            assert False

        if pid == 0:
            self.execution_context = self.ExecutionContext.worker
            error_log.debug('Closing listening socket in child.')
            self.sock.close()
        else:
            error_log.info('Spawned child process with pid %d', pid)
            error_log.debug('Closing client socket in parent process')

            try:
                client_socket.close()
            except OSError:
                error_log.exception('Failed to clean up client socket in '
                                    'parent process.')
            self.workers.append(self.ActiveWorker(pid=pid,
                                                  created_on=time.time()))

        return pid

    @staticmethod
    def work(client_socket, address, quick_reply_with=None):
        with Worker(client_socket, address) as worker:
            if quick_reply_with:
                worker.respond(quick_reply_with, closing=True)
                return

            while worker.http_connection_is_open:
                request = worker.parse_request()
                response = handle_request(request)
                worker.respond(response)

    # noinspection PyUnusedLocal
    def terminate_hanged_workers(self, signum, stack_frame):
        assert signum == signal.SIGALRM

        error_log.debug('Caught SIGALRM signal to join and terminate workers')

        leftover_workers = collections.deque()

        for worker in self.workers:
            # TODO this might raise OSError
            has_finished, status = os.waitpid(worker.pid, os.WNOHANG)

            if has_finished:
                error_log.info('Worker %s has finished.', worker)
            else:
                leftover_workers.append(worker)

        self.workers = leftover_workers
        error_log.info('%d active workers left. Searching for hanged workers.',
                       len(self.workers))

        now = time.time()
        for worker in self.workers:
            if now - worker.created_on > self.process_timeout:
                os.kill(worker.pid, signal.SIGTERM)
                error_log.info('Sent SIGTERM to hanged worker %s', worker)

        error_log.debug('Rescheduling SIGALRM signal to after %s seconds',
                        self.process_timeout)
        signal.alarm(self.process_timeout)


class Worker:
    """ Receives/parses requests and sends/encodes back responses.

    Instances of this class MUST be used through a context manager to ensure
    proper clean up of resources.

    The methods self.parse_request() and self.respond() MUST only be used
    when the property self.http_connection_is_open is True.

    Persistent connections are handled, but require self.parse_request() to be
    repeatedly called.
    """

    # noinspection PyUnusedLocal
    def __init__(self, sock, address):
        self.sock = sock
        self.last_request = None
        self.last_response = None
        self.responding = False

        signal.signal(signal.SIGTERM, self.handle_termination)
        sock.settimeout(config.getint('http', 'request_timeout'))

        self.request_receiver = RequestReceiver(sock)

    @property
    def http_connection_is_open(self):
        def client_closed_connection(request):
            if not request:
                return False
            elif 'Connection' not in request.headers:
                return False
            else:
                pass

            c = request.headers['Connection']
            hop_by_hop_headers = (h.strip() for h in c.split(b','))

            return b'close' in hop_by_hop_headers

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

        return not (client_closed_connection(request=self.last_request) or
                    server_closed_connection(response=self.last_response))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not exc_val:
            error_log.info('Cleaning up worker after successful execution.'
                           ' Shutting down socket %d for both r/w',
                           self.sock.fileno())
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
            return False

        if self.responding:
            error_log.error(
                'An exception occurred after worker had sent bytes over'
                ' the socket. Client will receive an invalid HTTP response.'
            )
            error_log.info('Shutting down socket %d for both r/w',
                           self.sock.fileno())
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
            return False
        elif not ws.err_responses.can_handle_err(exc_val):
            error_log.error(
                'An exception occurred that the worker cannot handle.'
                ' Client will not receive an HTTP response.'
            )
            error_log.info('Shutting down socket %d for both r/w',
                           self.sock.fileno())
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
            return False
        else:
            pass

        response = ws.err_responses.handle_err(exc_val)
        response.headers['Connection'] = 'close'
        try:
            self.respond(response)
        except OSError as e:
            error_log.critical('Caught OSError with errno %d', e.errno)
            if e.errno == errno.ECONNRESET:
                error_log.warning('Client stopped listening prematurely.'
                                  ' (no Connection: close header was received)')
                self.sock.close()
                return True
            else:
                raise

        error_log.debug('Shutting down and closing client socket %d',
                        self.sock.fileno())
        # TODO the socket needs to be shutdown for reading as well, but
        # only after the client has received this response ?
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError as e:
            if e.errno == errno.ENOTCONN:
                error_log.warning('Got ENOTCONN when shutting down socket.')
            else:
                raise
        self.sock.close()

        return True

    def parse_request(self):
        assert self.http_connection_is_open

        self.last_request = ws.http.parser.parse(self.request_receiver)
        return self.last_request

    def respond(self, response, closing=False):
        assert self.http_connection_is_open

        # TODO there needs to be a way to send Close connection through here.
        # instead of timing out and getting terminated.
        if closing:
            response.headers['Connection'] = 'close'

        self.responding = True
        response.send(self.sock)
        self.last_response = response
        self.responding = False

    # noinspection PyUnusedLocal
    def handle_termination(self, signum, stack_info):
        assert signum == signal.SIGTERM

        error_log.info('Parent process requested termination.'
                       ' Cleaning up as much as possible and'
                       ' and sending a service unavailable response')

        # No salvation if bytes have already been sent over the socket
        assert not self.responding

        response = ws.err_responses.service_unavailable()
        response.headers['Connection'] = 'close'
        response.send(self.sock)

        # TODO what kind of error is this ?
        # raise PeerError(msg='Parent process requested termination.',
        #                 code='PROCESSING_TIMED_OUT')
        raise RuntimeError('Parent process requested termination.')


class RequestReceiver:
    buffer_size = 2048

    def __init__(self, sock):
        self.sock = sock
        self.chunks = []
        self.current_chunk = None
        self.socket_broke = False

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

        try:
            chunk = self.sock.recv(self.__class__.buffer_size)
        except socket.timeout as e:
            error_log.exception('Socket timed out while receiving request.')
            raise PeerError(msg='Waited too long for a request',
                            code='RECEIVING_REQUEST_TIMED_OUT') from e

        error_log.debug('Read chunk %s', chunk)

        if chunk == b'':
            error_log.info('Socket %d broke', self.sock.fileno())
            self.socket_broke = True
            raise PeerError(code='PEER_STOPPED_SENDING',
                            msg='Client send 0 bytes through socket.')

        self.chunks.append(chunk)
        self.current_chunk = iter(chunk)

        return next(self.current_chunk)


def handle_request(request):
    file_path = request.request_line.request_target.path
    return ws.serve.get_file(file_path)


def main():
    # Main process should never exit from the server.listen() loop unless
    # an exception occurs.
    with Server() as server:
        server.listen()


if __name__ == '__main__':
    main()
