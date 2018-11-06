import io
import os
import re

import ws.http.utils
from ws.config import config
from ws.err import *
from ws.http.structs import HTTPResponse, HTTPStatusLine, HTTPHeaders
from ws.logs import error_log

STATIC_ROUTE = config['routes']['static']
STATIC_DIR = os.path.realpath(os.path.abspath(
    config['resources']['static_dir']
))

error_log.info('Configured static route is %s. Directory is %s',
               STATIC_ROUTE, STATIC_DIR)

if not STATIC_ROUTE.endswith('/'):
    raise SysError(msg="routes.static must end with a '/'",
                   code='CONFIG_BAD_STATIC_ROUTE')
if not os.path.isdir(STATIC_DIR):
    raise SysError(msg='resources.static_dir field must be a directory',
                   code='CONFIG_BAD_STATIC_DIR')


class StaticFiles:
    def __init__(self, document_root=STATIC_DIR, route_prefix=STATIC_ROUTE):
        self.document_root = document_root
        self.route_prefix = route_prefix
        self.file_keys = frozenset()
        self.reindex_is_scheduled = False

    # noinspection PyUnusedLocal
    def schedule_reindex(self, signum, stack_frame):
        """ Method to be used as a signal handler to avoid race conditions."""
        error_log.debug3('Scheduled reindex.')
        self.reindex_is_scheduled = True

    def reindex_files(self):
        error_log.info('Reindexing files under dir %s', self.document_root)
        file_keys = set()
        for dir_path, dir_names, file_names in os.walk(self.document_root):
            for fn in file_names:
                fp = os.path.join(dir_path, fn)
                stat = os.stat(fp)
                file_keys.add((stat.st_ino, stat.st_dev))
        self.file_keys = frozenset(file_keys)
        error_log.debug3('Indexed file keys are: %s', self.file_keys)
        self.reindex_is_scheduled = False

    def get_route(self, route):
        if self.reindex_is_scheduled:
            self.reindex_files()

        resolved = self.resolve_route(route)

        if not resolved:
            return ws.http.utils.build_response(404)

        try:
            body_it = file_chunk_gen(resolved)
            # startup the generator to have exceptions blow here.
            next(body_it)
            return HTTPResponse(
                status_line=HTTPStatusLine(http_version='HTTP/1.1',
                                           status_code=200,
                                           reason_phrase=''),
                headers=HTTPHeaders({
                    'Content-Length': os.path.getsize(resolved)
                }),
                body=body_it
            )
        except (FileNotFoundError, IsADirectoryError):
            return ws.http.utils.build_response(404)

    def resolve_route(self, route):
        if self.reindex_is_scheduled:
            self.reindex_files()

        route = ws.http.utils.decode_uri_component(route)

        if not route.startswith(self.route_prefix):
            error_log.debug('Route %s does not start with prefix %s',
                            route, self.route_prefix)
            return None
        elif route == self.route_prefix:
            return config.get('resources', 'index_page', fallback=None)

        rel_path = route[len(self.route_prefix):]
        file_path = os.path.abspath(os.path.join(self.document_root, rel_path))

        try:
            stat = os.stat(file_path)
            file_key = (stat.st_ino, stat.st_dev)
        except FileNotFoundError:
            return None

        if file_key not in self.file_keys:
            error_log.debug('File key %s of route %s is not inside indexed '
                            'file keys.')
            return None

        return file_path


def file_chunk_gen(fp):
    buf_size = 4096
    with open(fp, mode='rb') as f:
        chunk = f.read(buf_size)
        # temporarily stop gen for exceptions to blow up before
        # content gets yielded
        yield

        while chunk:
            yield chunk
            chunk = f.read(buf_size)


def is_status_route(route):
    status_route = config.get('routes', 'status', fallback=None)

    if not status_route:
        error_log.debug2('Status route is not set.')
        return False

    status_route = ws.http.utils.normalized_route(status_route)
    route = ws.http.utils.normalized_route(route)
    return status_route == route


FORMAT_FIELD_REGEX = re.compile('%\((\w*)\)\w')


def worker_status(request_stats):
    error_log.debug('Serving worker stats.')
    body = []
    for stat_name, stat_entry in request_stats.items():
        total, count = stat_entry['total'], stat_entry['count']
        if count:
            line = '{}:{}'.format(stat_name, total / count).encode('ascii')
            body.append(line)
    body = b'\n'.join(body)
    ib = io.BytesIO()
    ib.write(body)
    ib.seek(0)
    return ws.http.utils.build_response(
        status_code=200,
        headers={'Content-Length': len(body),
                 'Content-Type': 'text/html'},
        body=ib
    )
