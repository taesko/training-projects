import collections


class BaseError(Exception):
    def __init__(self, *, msg, code):
        super().__init__(msg)

        self.msg = msg
        self.code = code

    @classmethod
    def assert_(cls, condition, *, msg, code, from_=None):
        if not condition:
            if from_:
                raise cls(msg=msg, code=code) from from_
            else:
                raise cls(msg=msg, code=code)


class PeerError(BaseError):
    pass


class SysError(BaseError):
    pass


class UserError(BaseError):
    pass


class ServerException(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)

        self.msg = msg
        self.code = code


class SignalReceivedException(ServerException):
    def __init__(self, msg, code, signum):
        super().__init__(msg=msg, code=code)
        self.signum = signum


class BrokenSocketException(ServerException):
    pass


class ExcHandler:
    """ Strategy pattern for handling exceptions.

    Instances of this class can register functions as exception handlers by
    decorating them.
    """

    def __init__(self):
        self.handlers = {}

    def __call__(self, exc_cls):
        def decorator(func):
            assert isinstance(func, collections.Callable)
            assert exc_cls not in self.handlers

            self.handlers[exc_cls] = func
            return func

        return decorator

    def can_handle(self, exc):
        return exc.__class__ in self.handlers

    def handle(self, exc):
        return self.handlers[exc.__class__](exc)
