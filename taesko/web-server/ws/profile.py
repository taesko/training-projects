import collections
import contextlib
import time

from ws.config import config


class ServerProfiler:
    def __init__(self):
        pass


class WorkerProfiler:
    def __init__(self):
        pass


class ConnectionProfiler:
    """ Profile a client connection.

    Monitors - total time between accept and close,
    """
    def __init__(self):
        self.accepted_on = None
        self.closed_on = None


class RequestProfiler:
    """ Profile a single request from a client.

    Monitors:
        time between recv() of first byte and send() of last byte.
        time for recv() and parse only.
        time for handling only.
        time for sending only.

    """
    def __init__(self):
        self.before_first_recv = None
        self.after_last_recv = None
        self.before_first_send = None
        self.after_last_send = None
        self.recv_times = []
        self.send_times = []
        self.handle_times = []
        self.timer = Timer()

    @contextlib.contextmanager
    def record_recv(self):
        pass

    @contextlib.contextmanager
    def record_send(self):
        pass

    @contextlib.contextmanager
    def record_handle(self):
        pass


class Timer:
    def __init__(self):
        self.times = collections.defaultdict(list)
        self.recording = {}

    def start(self, key):
        assert key not in self.recording
        self.recording[key] = time.clock_gettime(time.CLOCK_MONOTONIC)

    def stop(self, key):
        end = time.clock_gettime(time.CLOCK_MONOTONIC)

        assert key in self.recording

        self.times[key].append(end - self.recording[key])

    @contextlib.contextmanager
    def time(self, key):
        self.start(key)
        yield
        self.stop(key)
