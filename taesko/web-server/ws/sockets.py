import array
import socket
import ssl
from ssl import Purpose
# noinspection PyUnresolvedReferences
from socket import (SHUT_WR, SHUT_RD, SHUT_RDWR, AF_INET, SOCK_STREAM,
                    SOL_SOCKET, SO_REUSEADDR)
from ws.logs import error_log

SHUTDOWN_MSGS = {
    SHUT_RD: 'Shutting down socket %d for reading/receiving.',
    SHUT_WR: 'Shutting down socket %d for writing/sending.',
    SHUT_RDWR: 'Shutting down socket %d for both r/w.'
}

TimeoutException = socket.timeout


class Socket(socket.socket):
    def client_uses_ssl(self):
        # taken from
        # https://stackoverflow.com/questions/36865867/how-to-check-if-a-connection-is-ssl
        return self.recv(1, socket.MSG_PEEK) == b'\x16'

    def shutdown(self, how, pass_silently=False):
        error_log.debug2(SHUTDOWN_MSGS[how], super().fileno())

        if pass_silently:
            try:
                super().shutdown(how)
            except OSError:
                pass
        else:
            super().shutdown(how)

    def close(self, pass_silently=False):
        error_log.debug2('Closing socket %d', self.fileno())

        if pass_silently:
            try:
                super().close()
            except OSError:
                pass
        else:
            super().close()

    def __repr__(self):
        return 'Socket(fileno={})'.format(self.fileno())


class SSLSocket(ssl.SSLSocket):
    @classmethod
    def from_sock(cls, sock, context, server_side=False,
                  do_handshake_on_connect=True,
                  suppress_ragged_eofs=True,
                  server_hostname=None):
        # noinspection PyArgumentList
        return cls(sock=sock, server_side=server_side,
                   do_handshake_on_connect=do_handshake_on_connect,
                   suppress_ragged_eofs=suppress_ragged_eofs,
                   server_hostname=server_hostname,
                   _context=context)

    def shutdown(self, how, pass_silently=False):
        error_log.debug2(SHUTDOWN_MSGS[how], super().fileno())

        if pass_silently:
            try:
                super().shutdown(how)
            except OSError:
                error_log.exception('Shutting down socket %s failed.',
                                    self.fileno())
        else:
            super().shutdown(how)

    def close(self, pass_silently=False):
        error_log.debug2('Closing socket %d', self.fileno())

        if pass_silently:
            try:
                super().close()
            except OSError:
                error_log.exception('Closing socket %s failed.',
                                    self.fileno())
        else:
            super().close()


class ServerSocket(Socket):
    def accept(self):
        """
        :rtype: ws.sockets.ClientSocket
        """
        s, a = super().accept()
        error_log.debug('Accepted connection from %s on socket %s',
                        a, s.fileno())

        return Socket(fileno=s.detach()), a

    def dup(self):
        # noinspection PyUnresolvedReferences
        s = super().dup()
        return Socket(fileno=s.detach())


class FDTransport:
    def __init__(self, sender_timeout=None, receiver_timeout=None):
        # TODO create and use this as a non-blocking pair.
        pair = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sender, self.receiver = pair
        if sender_timeout:
            self.sender.settimeout(sender_timeout)
        if receiver_timeout:
            self.receiver.settimeout(receiver_timeout)
        self.fixed_msg_len = 50
        self.max_fds = 5
        self._mode = None

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, val):
        assert self._mode is None
        assert val in ('sender', 'receiver')

        if val == 'sender':
            self.receiver.close()
        else:
            self.sender.close()

        self._mode = val

    def fileno(self):
        assert self._mode is not None
        socks = {
            'sender': self.sender,
            'receiver': self.receiver
        }
        return socks[self._mode].fileno()

    def send_fds(self, msg, fds):
        assert self._mode == 'sender'
        afds = array.array('i', fds)
        anc_data = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, afds.tobytes())]
        self.sender.sendmsg([msg], anc_data)

    def recv_fds(self):
        assert self._mode == 'receiver'
        fds = array.array('i')
        msg, ancdata, msg_flags, address = self.receiver.recvmsg(
            self.fixed_msg_len, socket.CMSG_LEN(self.max_fds * fds.itemsize)
        )

        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if (cmsg_level == socket.SOL_SOCKET and
                    cmsg_type == socket.SCM_RIGHTS):
                truncated = len(cmsg_data) % fds.itemsize
                fds.frombytes(cmsg_data[:len(cmsg_data) - truncated])

        return msg, fds

    def discard(self):
        try:
            self.sender.close()
        except OSError:
            pass
        try:
            self.receiver.close()
        except OSError:
            pass


def create_default_ssl_context(purpose=Purpose.SERVER_AUTH, **kwargs):
    return ssl.create_default_context(purpose, **kwargs)


def randomize_ssl_after_fork():
    ssl.RAND_add(ssl.RAND_bytes(10), 0.0)
