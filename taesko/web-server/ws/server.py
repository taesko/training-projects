import cProfile
import contextlib
import enum
import errno
import io
import json
import os
import pstats
import subprocess
import time

import ws.cworker
import ws.logs
import ws.sockets
import ws.signals
import ws.worker
from ws.config import config
from ws.err import *
from ws.logs import error_log, profile_log

SERVER_PROFILING_ON = config.getboolean('profiling', 'on_server')
WORKER_PROFILING_ON = config.getboolean('profiling', 'on_workers')

default_signal_handler_depreciated = ws.signals.raising_signal_handler
ws.signals.signal(ws.signals.SIGTERM, ws.signals.raising_signal_handler)


class Server:
    class ExecutionContext(enum.Enum):
        main = 'main'
        worker = 'worker'

    ActiveWorker = collections.namedtuple('ActiveWorker',
                                          ['pid', 'created_on', 'fd_transport'])

    def __init__(self):
        if not sys_has_fork_support():
            raise SysError(code='FORK_NOT_IMPLEMENTED',
                           msg="Kernel or C lib versions don't have "
                               "fork() support.")

        self.host = config['settings']['host']
        self.port = config.getint('settings', 'port')
        self.tcp_backlog_size = config.getint('settings', 'tcp_backlog_size')
        self.process_count_limit = config.getint('settings',
                                                 'process_count_limit')
        self.execution_context = self.ExecutionContext.main

        self.sock = ws.sockets.ServerSocket(ws.sockets.AF_INET,
                                            ws.sockets.SOCK_STREAM)
        self.sock.setsockopt(ws.sockets.SOL_SOCKET, ws.sockets.SO_REUSEADDR, 1)
        self.accepted_connections = 0
        self.workers = {}
        self.reaping = False
        self.reaped_pids = set()
        self.received_signals = set()
        ws.signals.signal(ws.signals.SIGCHLD, self.reap_children)
        ws.signals.signal(ws.signals.SIGUSR1, self.receive_signal)

    def setup(self):
        """ Bind socket and pre-fork workers. """
        error_log.info('Binding server on %s:%s', self.host, self.port)
        self.sock.bind((self.host, self.port))
        self.fork_workers(self.process_count_limit)

    def cleanup(self):
        """ Closing listening socket and reap children.

        This method sleeps for the maximum timeout of SIGTERM signal sent to
        a worker. (Worker.sigterm_timeout)
        """
        # don't cleanup workers because their sockets were already closed
        # during the self.fork_worker() call
        if self.execution_context == self.ExecutionContext.worker:
            return

        error_log.info("Closing server's listening socket")
        try:
            self.sock.close()
        except OSError:
            error_log.exception('close() on listening socket failed.')

        ws.signals.signal(ws.signals.SIGCHLD, ws.signals.SIG_DFL)
        active_workers = [worker for worker in self.workers.values()
                          if worker.pid not in self.reaped_pids]
        for worker in active_workers:
            worker.terminate()

        if active_workers:
            timeout = max(worker.sigterm_timeout for worker in active_workers)
            error_log.info('Waiting %s seconds for children to finish.',
                           timeout)
            time.sleep(timeout)

        for worker in active_workers:
            pid, exit = os.waitpid(worker.pid, os.WNOHANG)
            if not pid:
                worker.kill_if_hanged()

    def listen(self):
        assert all(isinstance(w, WorkerProcess)
                   for w in self.workers.values())
        assert len(self.workers) == self.process_count_limit

        error_log.info('Listening with backlog %s...', self.tcp_backlog_size)
        self.sock.listen(self.tcp_backlog_size)

        while True:
            # TODO add rate limiting on rate of clients sending connections
            # instead of on the HTTP syntax (which will be deferred to workers)
            try:
                sock, address = self.sock.accept()
            except OSError as err:
                error_log.warning('accept() raised ERRNO=%s with MSG=%s',
                                  err.errno, err.strerror)

                # TODO perhaps reopen failed listening sockets.
                assert err.errno not in (errno.EBADF, errno.EFAULT,
                                         errno.EINVAL, errno.ENOTSOCK,
                                         errno.EOPNOTSUPP)
                # don't break the listening loop just because one accept failed
                continue

            self.accepted_connections += 1
            passed = self.distribute_connection(client_socket=sock,
                                                address=address)
            if not passed:
                # TODO fork and reply quickly with a 503
                error_log.warning('Could not distribute connection %s / %s to '
                                  'workers. Dropping connection.',
                                  sock, address)

            sock.close(pass_silently=True)

            # duplicate the set so SIGCHLD handler doesn't cause problems
            to_remove = frozenset(self.reaped_pids)

            for pid in to_remove:
                old_worker = self.workers.pop(pid)
                old_worker.close_ipc()
                self.reaped_pids.remove(pid)

            # call outside of loop to avoid a race condition where a
            # worker_process get's forked with a pid in self.reaped_pids
            missing = self.process_count_limit - len(self.workers)
            if missing > 0:
                self.fork_workers(missing)

            for worker_process in self.workers.values():
                worker_process.kill_if_hanged()

            if ws.signals.SIGUSR1 in self.received_signals:
                for pid in self.workers:
                    ws.signals.kill(pid, ws.signals.SIGUSR1)
                self.received_signals.remove(ws.signals.SIGUSR1)

    def distribute_connection(self, client_socket, address):
        for i, worker in enumerate(self.workers_round_robin()):
            if not worker.can_work():
                continue

            try:
                worker.send_connections([(client_socket, address)])
                return True
            except OSError as err:
                if worker.pid in self.reaped_pids:
                    continue
                error_log.warning('sending file descriptors to worker %s '
                                  'raised ERRNO=%s with MSG=%s',
                                  worker, err.errno, err.strerror)
                worker.terminate()

        return False

    def workers_round_robin(self):
        assert len(self.workers) > 0

        workers = tuple(self.workers.values())
        round_robin_offset = self.accepted_connections % len(workers)
        first = workers[round_robin_offset:]
        last = tuple(reversed(workers[:round_robin_offset]))
        return first + last

    def fork_workers(self, count=1):
        error_log.info('Forking %s workers', self.process_count_limit)
        assert isinstance(count, int)

        for _ in range(count):
            fd_transport = ws.sockets.FDTransport()
            pid = os.fork()
            if pid:
                self.execution_context = self.ExecutionContext.main
                error_log.debug('Forked worker with pid=%s', pid)
                ws.sockets.randomize_ssl_after_fork()
                fd_transport.mode = 'sender'
                wp = WorkerProcess(pid=pid, fd_transport=fd_transport)
                self.workers[wp.pid] = wp
            else:
                self.execution_context = self.ExecutionContext.worker
                ws.signals.reset_handlers(excluding={ws.signals.SIGTERM})
                ws.signals.signal(ws.signals.SIGCHLD, ws.signals.SIG_IGN)
                exit_code = 0
                worker = None
                # noinspection PyBroadException
                try:
                    ws.logs.setup_worker_handlers()
                    fd_transport.mode = 'receiver'
                    for other_worker in self.workers.values():
                        other_worker.close_ipc()
                    self.sock.close()
                    os.close(0)
                    os.close(1)
                    os.close(2)
                    with profile(WORKER_PROFILING_ON):
                        worker = ws.worker.Worker(fd_transport=fd_transport)
                        exit_code = worker.work()
                except SignalReceivedException as err:
                    error_log.info('Exiting from worker due to signal=%s',
                                   ws.signals.Signals(err.signum).name)
                except KeyboardInterrupt as err:
                    error_log.info('Exiting from worker due to '
                                   'KeyboardInterrupt')
                except BaseException:
                    error_log.exception('Worker failed.')
                    exit_code = 1
                finally:
                    if worker:
                        # noinspection PyBroadException
                        try:
                            worker.cleanup()
                        except BaseException:
                            error_log.exception('Cleanup of worker failed.')

                # noinspection PyProtectedMember
                os._exit(exit_code)

    # noinspection PyUnusedLocal
    def receive_signal(self, signum, stack_frame):
        self.received_signals.add(signum)

    # noinspection PyUnusedLocal
    def reap_children(self, signum, stack_frame):
        # TODO use a lock
        error_log.debug3('reap_children() called.')
        if self.reaping:
            return

        self.reaping = True

        try:
            while True:
                pid, exit_indicator = os.waitpid(-1, os.WNOHANG)
                if pid:
                    error_log.debug('Reaped pid %s', pid)
                    assert pid not in self.reaped_pids
                    self.reaped_pids.add(pid)
                else:
                    break
        except OSError as err:
            error_log.debug('During reaping of zombie child: wait() sys call '
                            'failed with ERRNO=%s and MSG=%s. This is mostly '
                            'not a problem.', err.errno, err.strerror)
        finally:
            self.reaping = False


class WorkerProcess:
    sigterm_timeout = config.getint('settings', 'process_sigterm_timeout')

    def __init__(self, pid, fd_transport):
        self.pid = pid
        self.fd_transport = fd_transport
        self.created_on = time.time()
        self.sent_sockets = 0
        self.sent_sigterm_on = None
        self.terminating = False

    def send_connections(self, connections):
        sockets, addresses = zip(*connections)
        msg = bytes(json.dumps(addresses), encoding='utf-8')
        fds = [cs.fileno() for cs in sockets]
        self.fd_transport.send_fds(msg=msg, fds=fds)
        self.sent_sockets += len(sockets)
        return True

    def can_work(self):
        return not self.terminating

    def terminate(self):
        if self.terminating:
            return

        try:
            self.sent_sigterm_on = time.time()
            os.kill(self.pid, ws.signals.SIGTERM)
            self.terminating = True
        except OSError as err:
            error_log.warning('Failed to sent SIGTERM to worker with pid %s. '
                              'ERRNO=%s and MSG=%s', err.errno, err.strerror)

    def kill_if_hanged(self, now=None):
        if not self.terminating:
            return False

        now = now or time.time()
        if now - self.sent_sigterm_on > self.sigterm_timeout:
            # don't fail if worker is already dead.
            try:
                ws.signals.kill(self.pid, ws.signals.SIGKILL)
            except OSError as err:
                error_log.warning('Killing worker with pid %s failed. '
                                  'ERRNO=%s and MSG=%s',
                                  err.errno, err.strerror)

        return True

    def close_ipc(self):
        self.fd_transport.discard()

    def __repr__(self):
        return 'WorkerProcess(pid={})'.format(self.pid)


def sys_has_fork_support():
    error_log.info('Checking if system has fork support by doing a '
                   'dummy fork...')
    try:
        pid = os.fork()
    except OSError as err:
        if err.errno == errno.ENOSYS:
            error_log.critical('System does not have fork() support.')
            return False
        else:
            return True

    if pid == 0:
        # noinspection PyProtectedMember
        os._exit(0)
    else:
        error_log.info('Fork successful. Cleaning up dummy child '
                       '(pid={:d})...'.format(pid))
        os.waitpid(pid, 0)

    return True


@contextlib.contextmanager
def profile(enabled=True):
    if not enabled:
        yield
        return

    error_log.info('Starting profiling.')
    profiler = cProfile.Profile()
    profiler.enable()
    profile_log.profile('Enabled profiling')
    try:
        yield
    finally:
        profiler.disable()
        profile_log.profile('Disabled profiling')
        s = io.StringIO()
        ps = pstats.Stats(profiler, stream=s)
        ps = ps.sort_stats('cumulative')
        ps.print_stats()
        profile_log.profile('cProfiler results:\n %s', s.getvalue())


def main():
    # Main process should never exit from the server.listen() loop unless
    # an exception occurs.
    fd_limit = subprocess.check_output(['ulimit', '-n'], shell=True)
    error_log.info('ulimit -n is "%s"', fd_limit.decode('ascii'))
    server = Server()
    server.setup()
    with profile(SERVER_PROFILING_ON):
        # noinspection PyBroadException
        try:
            server.listen()
        except SignalReceivedException as err:
            if err.signum == ws.signals.SIGTERM:
                error_log.info('SIGTERM signal broke listen() loop.')
            else:
                error_log.exception('Unknown signal broke listen() loop.')
        except KeyboardInterrupt:
            error_log.info('KeyboardInterrupt broke listen() loop.')
        except BaseException:
            error_log.exception('Unhandled exception broke listen() loop.')
        finally:
            server.cleanup()
