import os
import select
import subprocess
import time

import ws.http.utils
import ws.sockets
from ws.config import config
from ws.err import *
from ws.http.utils import normalized_route
from ws.logs import error_log

CGI_SCRIPTS_DIR = config['cgi']['scripts_dir']

if not os.path.isdir(CGI_SCRIPTS_DIR):
    raise UserError(msg='In configuration: cgi.scripts_dir is not a directory.',
                    code='CGI_CONFIG_NOT_DIR')


class CGIException(ServerException):
    pass


class CGIScript(collections.namedtuple('CGIScript', ['name',
                                                     'route',
                                                     'pass_full_path_info',
                                                     'timeout'])):
    @classmethod
    def from_config(cls, script_name):
        section_name = 'cgi_{}'.format(script_name)
        if section_name not in config:
            msg = 'CGI script with name {} is not configured'.format(
                script_name
            )

            raise SysError(msg=msg, code='CGI_CONF_MISSING_SECTION')

        section = config[section_name]

        cgi_script = cls(
            name=script_name,
            route=normalized_route(section['route']),
            pass_full_path_info=section.getboolean('pass_full_path_info'),
            timeout=section.getint('timeout')
        )

        # noinspection PyUnresolvedReferences
        if not os.path.exists(cgi_script.script_path):
            # noinspection PyUnresolvedReferences
            msg = 'CGI executable not found at {}'.format(
                cgi_script.script_path
            )
            raise SysError(msg=msg, code='CGI_CONF_NO_EXEC')

        return cgi_script

    @property
    def script_path(self):
        return os.path.join(CGI_SCRIPTS_DIR, self.name)


def cgi_config():
    scripts = {}
    scripts_dir = config['cgi']['scripts_dir']

    for file_name in os.listdir(scripts_dir):
        script_conf_section = 'cgi_{}'.format(file_name)

        if script_conf_section not in config:
            error_log.warning('CGI script %s is not configured and it will '
                              'not be run.', file_name)
            continue

        route = config[script_conf_section]['route']
        msg = "Scripts {fn_1} and {fn_2} are in conflict for route {route}."

        if route in scripts:
            raise SysError(msg=msg.format(fn_1=file_name, fn_2=scripts[route],
                                          route=route),
                           code='CGI_CONFIG_CONFLICT')

        script = CGIScript.from_config(file_name)
        scripts[script.route] = script

    return scripts


def find_cgi_script(uri):
    assert uri.is_in_absolute_form or uri.is_in_origin_form

    highest_match = (None, 0)

    uri_path = uri.path

    if not uri.path.endswith('/'):
        uri_path += '/'

    for script_route in CGI_SCRIPTS:
        if not uri_path.startswith(script_route):
            continue

        sr_parts = script_route.split('/')
        path_parts = uri_path.split('/')
        matching = sum(1 for path_part, sr_part in zip(path_parts, sr_parts)
                       if path_part == sr_part)

        if matching > highest_match[1]:
            highest_match = (script_route, matching)

    if not highest_match[0]:
        return None
    else:
        return CGI_SCRIPTS[highest_match[0]]


def compute_path_info(cgi_script, uri):
    assert isinstance(cgi_script, CGIScript)
    assert uri.is_in_absolute_form or uri.is_in_origin_form

    if cgi_script.pass_full_path_info:
        encoded_path_info = uri.path
    else:
        cgi_route = normalized_route(cgi_script.route)
        request_route = normalized_route(uri.path)

        assert request_route.startswith(cgi_route)

        encoded_path_info = request_route[len(cgi_route) - 1:]

    # TODO should this raise errors if url decoding creates extre '/' ?
    return ws.http.utils.decode_uri_component(encoded_path_info)


def compute_path_translated(path_info):
    # TODO document root ?
    return os.path.abspath(path_info)


def compute_query_string(uri):
    if not uri.query:
        return ''
    else:
        return uri.query


def compute_http_headers_env(request):
    env = {}

    skipped_headers = ('Content-Length', 'Content-Type')

    for header, value in request.headers.items():
        if header in skipped_headers:
            continue
        env_name = 'HTTP_' + header.upper().strip().replace('-', '_')
        folded_val = value.replace(b'\n', b' ').strip(b' ').decode('ascii')
        env[env_name] = folded_val

    return env


def can_handle_request(request):
    return bool(find_cgi_script(request.request_line.request_target))


def prepare_cgi_script_env(request, client_socket):
    assert can_handle_request(request)

    uri = request.request_line.request_target
    cgi_script = find_cgi_script(uri)

    script_env = compute_http_headers_env(request)

    script_env['GATEWAY_INTERFACE'] = 'CGI/1.1'
    script_env['PATH_INFO'] = compute_path_info(cgi_script, uri)
    script_env['PATH_TRANSLATED'] = compute_path_translated(
        script_env['PATH_INFO']
    )
    script_env['QUERY_STRING'] = compute_query_string(uri)
    script_env['REMOTE_ADDR'] = client_socket.getpeername()[0]
    # TODO script_env['REMOTE_HOST'] =
    script_env['REQUEST_METHOD'] = request.request_line.method
    script_env['SCRIPT_NAME'] = cgi_script.route
    script_env['SERVER_NAME'] = client_socket.getsockname()[0]
    script_env['SERVER_PORT'] = str(client_socket.getsockname()[1])
    script_env['SERVER_PROTOCOL'] = 'HTTP/1.1'
    script_env['SERVER_SOFTWARE'] = 'web-server-v0.3.0rc'

    if request.body:
        cl = request.headers.get('Content-Length', 0)
        script_env['Content-Length'] = str(cl)

        if 'Content-Encoding' in request.headers:
            script_env['Content-Encoding'] = request.headers['Content-Encoding']

    return script_env


def execute_script(request, client_socket):
    assert can_handle_request(request)

    uri = request.request_line.request_target
    cgi_script = find_cgi_script(uri)
    error_log.info('Executing CGI script %s', cgi_script.name)
    script_env = prepare_cgi_script_env(request, client_socket)
    error_log.debug('CGIScript environment will be: %s', script_env)

    has_body = 'Content-Length' in request.headers

    if has_body:
        stdin = subprocess.PIPE
    else:
        stdin = client_socket.fileno()

    try:
        proc = subprocess.Popen(
            args=os.path.abspath(cgi_script.script_path),
            env=script_env,
            stdin=stdin,
            stdout=client_socket.fileno()
        )
    except (OSError, ValueError):
        error_log.exception('Failed to open subprocess for cgi script {}'
                            .format(cgi_script.name))
        return ws.http.utils.build_response(500)

    if not has_body:
        client_socket.close(pass_silently=True)
        return

    error_log.debug('Request to CGI has body. Writing to stdin...')

    # noinspection PyBroadException
    try:
        length = int(request.headers['Content-Length'])
        body = (next(client_socket) for _ in range(length))
        chunk_size = 4096
        while True:
            try:
                chunk = bytes(b for i, b in enumerate(body) if i < chunk_size)
            except ws.sockets.ClientSocketException as err:
                error_log.warning('Encountered socket error while writing '
                                  'body - %s', err)
                break

            if chunk:
                error_log.debug3('Writing chunk %s', chunk)
                proc.stdin.write(chunk)
            else:
                break
    except ValueError:
        pass
    except Exception:
        error_log.exception('Failed to write body to CGI script.')
    finally:
        client_socket.close(pass_silently=True)
        try:
            proc.stdin.close()
        except OSError as err:
            error_log.warning('Closing CGI stdin pipe failed. ERRNO=%s MSG=%s.',
                              err.errno, err.strerror)


def stdout_chunk_iterator_depreciated(byte_iterator, *, timeout, stdin, stdout,
                                      in_chunk=4096, out_chunk=4096):
    assert isinstance(timeout, int)
    assert isinstance(stdin, int)
    assert isinstance(stdout, int)
    assert isinstance(byte_iterator, collections.Iterable)

    byte_iterator = iter(byte_iterator)
    has_more_input = False
    current_chunk = None
    currently_written = 0
    start = time.time()
    while time.time() - timeout < start:
        rlist, wlist, xlist = [stdout], [], []
        if has_more_input:
            wlist.append(stdin)
        else:
            os.close(stdin)

        # because current process serves only one request at once
        # there is no need to have a timeout on this select
        # if it blocks forever a process manager is going to kill it.
        rlist, wlist, xlist = select.select(rlist, wlist, xlist)
        if stdout in rlist:
            out = os.read(stdout, out_chunk)
            if not out:
                break
            yield out
        if stdin in wlist:
            if not current_chunk:
                current_chunk = b''.join(b for c, b in byte_iterator
                                         if c < in_chunk)
                currently_written = 0
            wrote = os.write(stdin, current_chunk[currently_written:])
            currently_written += wrote


CGI_SCRIPTS = cgi_config()
