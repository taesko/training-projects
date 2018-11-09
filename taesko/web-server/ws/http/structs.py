import collections

HTTPRequest = collections.namedtuple('HTTPRequest', ['request_line',
                                                     'headers'])


class HTTPRequestLine(collections.namedtuple('HTTPStartLine',
                                             ['method',
                                              'request_target',
                                              'http_version'])):
    def __str__(self):
        return ' '.join(map(str, self))

    def __bytes__(self):
        return str(self).encode('ascii')


class URI(collections.namedtuple('URI', ['protocol', 'host', 'port',
                                         'path', 'query'])):
    @property
    def is_in_origin_form(self):
        return not self.protocol and not self.host and self.path

    @property
    def is_in_absolute_form(self):
        return bool(self.protocol)

    @property
    def is_in_authority_form(self):
        return not self.protocol and self.host

    @property
    def is_in_asterisk_form(self):
        return not self.protocol and not self.host and not self.path

    def __str__(self):
        query_part = '?' + self.query if self.query else ''
        port_part = ':' + self.port if self.port else ''
        templates = {
            '{path}{query_part}': self.is_in_origin_form,
            '{protocol}://{host}{port_part}{path}{query_part}':
                self.is_in_absolute_form,
            '{host}{port_part}': self.is_in_authority_form,
            '*': self.is_in_asterisk_form
        }

        for template, is_right in templates.items():
            if is_right:
                return template.format(**locals(), **self._asdict())

    def __bytes__(self):
        return str(self).encode('ascii')


HTTPResponse = collections.namedtuple('HTTPResponse', ['status_line',
                                                       'headers',
                                                       'body'])


class HTTPStatusLine(
    collections.namedtuple('HTTPStatusLine', ['http_version',
                                              'status_code',
                                              'reason_phrase'])
):
    def __bytes__(self):
        return str(self).encode('ascii')

    def __str__(self):
        template = '{self.http_version} {self.status_code} {self.reason_phrase}'
        return template.format(self=self)


class HTTPHeaders(collections.UserDict):
    def __bytes__(self):
        return str(self).encode('ascii')

    def __str__(self):
        lines = ('{}:{}'.format(field, value) for field, value in self.items())
        return '\r\n'.join(lines)
