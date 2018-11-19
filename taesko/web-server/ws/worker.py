import errno
import select

import ws.auth
import ws.cworker
import ws.http.utils as hutils
import ws.ratelimit
import ws.sockets
import ws.signals
import ws.serve
from ws.err import *
from ws.config import config
from ws.logs import error_log


class Worker:
    def __init__(self, fd_transport, parent_ctx=None):
        parent_ctx = parent_ctx or {}

        assert isinstance(fd_transport, ws.sockets.FDTransport)
        assert isinstance(parent_ctx, collections.Mapping)

        self.connection_workers = {}
        self.request_stats = collections.defaultdict(lambda: {'total': 0,
                                                              'count': 0})
        self.parent_ctx = parent_ctx
        self.fd_transport = fd_transport
        self.rate_controller = ws.ratelimit.HTTPRequestRateController()
        self.auth_scheme = ws.auth.BasicAuth()
        self.static_files = ws.serve.StaticFiles()
        self.static_files.reindex_files()
        ws.signals.signal(ws.signals.SIGUSR1,
                          self.static_files.schedule_reindex)

        if config.getboolean('ssl', 'enabled'):
            cert_file = config['ssl']['cert_file']
            purpose = ws.sockets.Purpose.CLIENT_AUTH
            self.ssl_ctx = ws.sockets.create_default_ssl_context(purpose)
            self.ssl_ctx.load_cert_chain(certfile=cert_file)
        else:
            self.ssl_ctx = None

        self.select_args = [{self.fd_transport.fileno()}, set(), set()]

    def recv_new_sockets(self):
        """ Receives new sockets from the parent process.

        This function RAISES the following exceptions:
        ws.sockets.TimeoutException - when the parent process takes to long to
            send a socket's file descriptor (can happen because he is
            blocked or because there are no clients connecting atm.)
        OSError - when a problem occurs with the underling UNIX socket. This is
            not a recoverable state and there is no guarantee that subsequent
            calls won't fail as well.
        """
        error_log.debug3('Receiving new sockets through fd transport.')
        try:
            msg, fds = self.fd_transport.recv_fds()
        except OSError as err:
            if err.errno == errno.EWOULDBLOCK:
                error_log.warning('No sockets received.')
                return []
            else:
                error_log.exception('fd_transport failed to recv_fds() with '
                                    'ERRNO=%s and MSG=%s',
                                    err.errno, err.strerror)
                return []

        connections = []

        for fd in fds:
            error_log.debug3('Received file descriptor %s', fd)
            sock = ws.sockets.Socket(fileno=fd)
            try:
                address = sock.getpeername()
            except OSError as err:
                msg = 'getpeername() failed with ERRNO=%s and MSG=%s'
                error_log.warning(msg, err.errno, err.strerror)
                continue

            if self.rate_controller.is_banned(ip_address=address[0]):
                sock.close(pass_silently=True)
            else:
                connections.append((sock, address))

        return connections

    def work(self):
        error_log.info('Entering endless loop of processing sockets.')

        while True:
            # TODO connection_workers is a horrible name
            connected_sockets = tuple(conn[0].fileno() for conn in
                                      self.connection_workers.keys())
            # for conn_worker in self.connection_workers:
            #     r, w = conn_worker.select_fds()
            #     self.select_args
            rlist = connected_sockets + (self.fd_transport.fileno(),)
            wlist = connected_sockets
            xlist = []  # TODO wat ?
            error_log.debug3('select() on descriptors %s, %s, %s',
                             rlist, wlist, xlist)
            try:
                # have select block indefinitely because our only purpose is to
                # work... forever...
                rlist, wlist, xlist = select.select(rlist, wlist, xlist, None)
            except OSError as err:
                error_log.warning('select() failed with ERRNO=%s and MSG=%s',
                                  err.errno, err.strerror)
                continue

            rset = frozenset(rlist)
            wset = frozenset(wlist)
            xset = frozenset(xlist)

            leftover_conn_workers = {}
            for conn, conn_worker in self.connection_workers.items():
                error_log.debug3('Processing connection %s', conn)
                sock, address = conn
                try:
                    conn_worker.process(readable_fds=rset,
                                        writable_fds=wset,
                                        failed_fds=xset)
                except (SignalReceivedException, KeyboardInterrupt):
                    raise
                except (ServerException, AssertionError):
                    error_log.exception('Unhandled exception in work() method. '
                                        'Socket=%s', sock)
                    sock.close(pass_silently=True)
                else:
                    if conn_worker.finished:
                        self.rate_controller.record_handled_connection(
                            ip_address=address[0],
                            status_codes=conn_worker.status_codes()
                        )
                        sock.close(pass_silently=True)
                    else:
                        leftover_conn_workers[conn] = conn_worker

            self.connection_workers = leftover_conn_workers
            error_log.debug2('Connections that still require processing are %s',
                             self.connection_workers.keys())

            if self.fd_transport.fileno() in rset:
                new_connections = self.recv_new_sockets()
                for sock, address in new_connections:
                    conn = (sock, address)
                    conn_worker = self.handle_connection(socket=sock,
                                                         address=address)
                    self.connection_workers[conn] = conn_worker

    def cleanup(self):
        error_log.info('Cleaning up... %s total leftover connections.',
                       len(self.connection_workers))
        self.fd_transport.discard()

        for sock, address in self.connection_workers:
            # noinspection PyBroadException
            try:
                res = hutils.build_response(503)
                self.handle_connection(socket=sock, address=address,
                                       quick_reply_with=res)
            except Exception:
                error_log.exception('Error while cleaning up client on '
                                    '%s / %s', sock, address)
            finally:
                sock.close(pass_silently=True)

    def handle_connection(self, socket, address, quick_reply_with=None,
                          ssl_only=config.getboolean('ssl', 'strict')):
        assert isinstance(socket, ws.sockets.Socket)
        assert isinstance(address, collections.Sequence)

        error_log.debug3('handle_connection()')
        wrapped_sock = socket

        if self.ssl_ctx:
            if socket.client_uses_ssl():
                wrapped_sock = ws.sockets.SSLSocket.from_sock(
                    sock=socket, context=self.ssl_ctx, server_side=True
                )
            elif ssl_only:
                quick_reply_with = hutils.build_response(403)
            else:
                error_log.info('Client on %s / %s does not use SSL/TLS',
                               socket, address)

        if quick_reply_with:
            # TODO
            raise NotImplementedError()
        # TODO worker_ctx={'request_stats': self.request_stats}
        # for exchange_stats in conn_worker.generate_stats():
        #     for stat_name, val in exchange_stats.items():
        #         self.request_stats[stat_name]['total'] += val
        #         self.request_stats[stat_name]['count'] += 1
        return ws.cworker.ConnectionWorker(sock=wrapped_sock, address=address,
                                           auth_scheme=self.auth_scheme,
                                           static_files=self.static_files,
                                           worker_stats={},
                                           persist_connection=True)


class IOLoop:
    def __init__(self):
        self.channels = {}
        self.read_fds = {}
        self.write_fds = {}

    def poll(self):
        while True:
            pass

    def subscribe(self, key, callback, read_fds, write_fds):
        assert key not in self.channels
        channel_read_fds = {}
        channel_write_fds = {}
        for fd in read_fds:
            assert fd not in self.read_fds
            self.read_fds[fd] = key
            channel_read_fds[fd] = False
        for fd in write_fds:
            assert fd not in self.write_fds
            self.write_fds[fd] = key
            channel_write_fds[fd] = True


    def unsubscribe(self, key):
        assert key in self.channels
        read_fds, write_fds, cb = self.channels.pop(key)
        for fd in self.read_fds:
            del read_fds[fd]
        for fd in self.write_fds:
            del write_fds[fd]
