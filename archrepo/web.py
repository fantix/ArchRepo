from base64 import b64encode
import gettext
import hashlib
import hmac
import logging
import cherrypy
from gevent.pywsgi import WSGIServer
from jinja2 import Environment, FileSystemLoader
import time
from gevent import monkey
import urllib2
import ujson
from archrepo import config
from pkg_resources import resource_filename


monkey.patch_socket()
monkey.patch_ssl()

FIELDS = ('id', 'name', 'arch', 'version', 'description', 'last_update')
USER_FIELDS = ('id', 'username', 'email', 'title', 'realname')


class AuthError(Exception):
    pass


class Auth(object):
    def __init__(self, pool):
        self.pool = pool

    def getUserInfo(self):
        #noinspection PyUnresolvedReferences
        return cherrypy.session.get('userinfo')

    def _login(self, *args, **kw):
        raise NotImplementedError(self)

    def login(self, *args, **kw):
        success, value = self._login(*args, **kw)
        if success:
            #noinspection PyUnresolvedReferences
            cherrypy.session['userinfo'] = value
        else:
            raise AuthError(value)


class FluxAuth(Auth):
    def __init__(self, pool):
        super(FluxAuth, self).__init__(pool)
        self._hmac_key = config.get('flux-sso', 'key')
        self._sso_api_url = config.get('flux-sso', 'api')
        self._sso_cookie = config.xget('flux-sso', 'cookie', default='xsdauth')

    def _login(self, *args, **kw):
        logging.debug('Trying to login with FluxBB cookie')
        auth_str = cherrypy.request.cookie.get(self._sso_cookie, None)
        if auth_str is None or auth_str.value == 'GUEST':
            return False, 'FluxBB is not logged in'

        auth_str = urllib2.urlparse.unquote(auth_str.value)
        uid, expires, sig = auth_str.split('#')
        if int(expires) < time.time():
            return False, 'Cookie timed out'

        msg = '#'.join((uid, expires))
        raw_hmac = hmac.new(self._hmac_key, msg, hashlib.sha256)
        new_sig = b64encode(raw_hmac.digest(), '-_').rstrip('=')
        if new_sig != sig:
            return False, 'Bad signature'

        with self.pool.cursor() as cur:
            cur.execute('SELECT %s FROM users WHERE id=%%s' %
                        ', '.join(USER_FIELDS), (uid,))
            result = cur.fetchone()
        if not result:
            logging.info('Linking new FluxBB user #%s', uid)
            data = urllib2.urlopen(self._sso_api_url % uid).read()
            info = ujson.loads(data)
            values = [uid]
            for field in USER_FIELDS[1:]:
                values.append(info.get(field))
            cur.execute('INSERT INTO users (%s) VALUES (%s) RETURNING %s' % (
                ', '.join(USER_FIELDS), ', '.join(['%s'] * len(USER_FIELDS)),
                ', '.join(USER_FIELDS)), tuple(values))
            result = cur.fetchone()
        userinfo = dict(zip(USER_FIELDS, result))
        return True, userinfo


class PackageList(object):
    def __init__(self, pool):
        self.pool = pool
        self._env = Environment(loader=FileSystemLoader(
            resource_filename('archrepo', 'templates')),
                                extensions=['jinja2.ext.i18n'])
        trans = gettext.translation('archrepo', '/home/fantix/PycharmProjects/archrepo/locale/')
        #noinspection PyUnresolvedReferences
        self._env.install_gettext_translations(trans)

        if config.has_section('flux-sso'):
            self.auth = FluxAuth(pool)
        else:
            self.auth = Auth(pool)

    @cherrypy.expose
    def query(self, orderby=None):
        userinfo = self.auth.getUserInfo()
        orderby_str = ''
        if orderby:
            orderby_list = []
            desc = True
            for part in orderby.lower().split(','):
                if part in ('last_update', 'name'):
                    orderby_list.append(part)
                if part == 'asc':
                    desc = False
            orderby_str = ' ORDER BY %s ' % (', '.join(orderby_list))
            if desc:
                orderby_str += 'DESC '

        with self.pool.cursor() as cur:
            cur.execute(
                'SELECT %s FROM packages WHERE latest %s' % (
                    ', '.join(FIELDS), orderby_str))
            result = [dict(zip(FIELDS, x)) for x in cur.fetchall()]
            tmpl = self._env.get_template('packages.html')
            return tmpl.render(packages=result, userinfo=userinfo)

    @cherrypy.expose
    def index(self):
        raise cherrypy.InternalRedirect('/query',
                                        query_string='orderby=last_update')


class ArchRepoWebServer(WSGIServer):
    def __init__(self, pool):
        host = config.xget('web', 'host', default='*')
        port = config.xgetint('web', 'port', default=8080)
        super(ArchRepoWebServer, self).__init__('%s:%d' % (host, port))
        cherrypy.server.unsubscribe()
        self.application = cherrypy.tree.mount(
            PackageList(pool), config={'/': {'tools.sessions.on': True}})
