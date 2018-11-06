import collections
import time

from ws.config import config
from ws.logs import error_log

CONSIDERED_CLIENT_ERRORS = (400,)

ClientConnectionRecord = collections.namedtuple(
    'ClientRecord', ['address', 'recorded_on', 'err_count']
)


class HTTPRequestRateController:
    """ Implements rate limiting on a per ip address basis.

    For this class to function properly handled http connections must be
    registered through record_handled_connection() after workers have finished
    with them.

    Banned ip addresses can be checked through is_banned() method.
    """
    max_recorded_addresses = 10
    max_recorded_connections = 10
    client_errors_threshold = config.getint(
        'http', 'client_errors_threshold'
    )
    ban_duration = config.getint(
        'http', 'ban_duration'
    )
    cleanup_threshold = 10000
    required_records = client_errors_threshold * 2

    def __init__(self):
        self.bad_connection_records = {}

    def is_banned(self, ip_address):
        assert isinstance(ip_address, (str, bytes))
        if ip_address not in self.bad_connection_records:
            return False

        record = self.bad_connection_records[ip_address]
        if time.time() - record['first_recorded_on'] > self.ban_duration:
            del self.bad_connection_records[ip_address]
            return False
        else:
            return record['err_count'] >= self.client_errors_threshold

    def record_handled_connection(self, ip_address, status_codes):
        assert isinstance(ip_address, (str, bytes))
        assert all(isinstance(sc, int) for sc in status_codes)

        err_count = sum(sc in CONSIDERED_CLIENT_ERRORS for sc in status_codes)

        if not err_count:
            return

        error_log.debug('Error count of address %s increased by %s.',
                        ip_address, err_count)

        if len(self.bad_connection_records) == self.cleanup_threshold:
            error_log.warning(
                "Reached max recorded addresses for rate limiting "
                "purposes. Dumping/freeing recorded bad connections."
            )
            self.bad_connection_records = {}
        if ip_address not in self.bad_connection_records:
            self.bad_connection_records[ip_address] = dict(
                ip_address=ip_address,
                first_recorded_on=time.time(),
                err_count=err_count
            )
        else:
            self.bad_connection_records[ip_address]['err_count'] += err_count
