import cherrypy
import gettext
import hashlib
import hmac
import logging
import math
import os
import sys
import time
import ujson
import urllib2
from babel.dates import format_date
from base64 import b64encode
from gevent import monkey
from gevent.pywsgi import WSGIServer
from pkg_resources import resource_filename
from jinja2 import Environment, FileSystemLoader

from archrepo import config
from archrepo.query import CursorPool, SubCursorPool


monkey.patch_socket()
monkey.patch_ssl()

FIELDS = ('id', 'name', 'arch', 'version', 'description', 'last_update',
          'flag_date', 'owner')
USER_FIELDS = ('id', 'username', 'email', 'title', 'realname')
AVAILABLE_LIMITS = ('25', '50', '100', '250', 'all')


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

    def logout(self, *args, **kw):
        #noinspection PyUnresolvedReferences
        cherrypy.session.pop('userinfo', None)


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


class ArchRepoApplication(object):
    def __init__(self, pool):
        self.cursorPool = CursorPool(
            config.xgetint('web', 'concurrent-queries', 16))
        self.pool = pool
        self._env = Environment(loader=FileSystemLoader(
            resource_filename('archrepo', 'templates')),
                                extensions=['jinja2.ext.i18n'])
        try:
            self.trans = gettext.translation(
                'archrepo', os.path.join(sys.prefix, 'share/locale'))
            #noinspection PyUnresolvedReferences
            self._env.install_gettext_translations(self.trans)
        except IOError:
            pass

        if config.has_section('flux-sso'):
            self.auth = FluxAuth(pool)
        else:
            self.auth = Auth(pool)

    def gettext(self, message):
        if hasattr(self, 'trans'):
            return self.trans.gettext(message).decode('utf-8')
        else:
            return message

    @cherrypy.expose
    def query(self, sort=None, arch=None, maintainer=None, q=None, limit=None,
              page=None, flagged=None, last_update=None):
        userinfo = self.auth.getUserInfo()

        sql = 'SELECT %s FROM packages' % ', '.join(FIELDS)
        sort_list = []
        where_list = ['latest']
        desc = True
        values = {}

        if arch is not None:
            if not isinstance(arch, list):
                arch = [arch]
            where_list.append('arch IN %(arch)s')
            values['arch'] = tuple(arch)
        else:
            arch = ()

        if last_update is not None and last_update.strip():
            where_list.append('last_update > %(last_update)s')
            values['last_update'] = last_update
        else:
            last_update = ''

        if flagged == '1':
            where_list.append('flag_date IS NOT NULL')
        elif flagged == '2':
            where_list.append('flag_date IS NULL')
        else:
            flagged = '0'

        if maintainer is not None and maintainer.isdigit():
            where_list.append('owner=%(owner)s')
            if int(maintainer):
                values['owner'] = int(maintainer)
            else:
                values['owner'] = None

        if sort:
            for part in sort.lower().split(','):
                if part in ('last_update', 'name', 'arch', 'flag_date'):
                    sort_list.append(part)
                if part in ('name', 'arch', 'asc'):
                    desc = False
                if part in ('desc',):
                    desc = True
        if not sort_list:
            sort = 'last_update'
            sort_list.append(sort)
            desc = True

        if q is not None and q.strip():
            sql = ', '.join((sql, "to_tsquery(%(lang)s, %(q)s) query"))
            values['lang'] = 'english'
            values['q'] = q
            where_list.append('searchable @@ query')
            sort_list.append('ts_rank_cd(searchable, query)')
            desc = True
        else:
            q = ''

        if where_list:
            sql = ' WHERE '.join((sql, ' AND '.join(where_list)))

        if sort_list:
            sql = ' ORDER BY '.join((sql, ', '.join(sort_list)))
            if desc:
                sql = ' '.join((sql, 'DESC'))

        if limit not in AVAILABLE_LIMITS[1:]:
            limit = AVAILABLE_LIMITS[0]
        if limit != 'all':
            #noinspection PyUnresolvedReferences
            if 'sub_cursor_pool' in cherrypy.session:
                #noinspection PyUnresolvedReferences
                cpool = cherrypy.session['sub_cursor_pool']
            else:
                cpool = SubCursorPool(
                    self.cursorPool,
                    config.xgetint('web', 'concurrent-queries-per-session', 1))
                #noinspection PyUnresolvedReferences
                cherrypy.session['sub_cursor_pool'] = cpool

            cursor = cpool.getCursor(self.pool, sql % values, sql, values)
            count = cursor.count
            all_pages = int(math.ceil(float(count) / int(limit)))
            if page is not None and page.isdigit():
                page = min(all_pages, max(1, int(page)))
            else:
                page = 1
            offset = (page - 1) * int(limit)
            result = cursor.fetch(int(limit), offset)
        else:
            page = 1
            all_pages = 1
            with self.pool.cursor() as cur:
                logging.debug('SQL: %s, VALUES: %r', sql, values)
                cur.execute(sql, values)
                result = cur.fetchall()
            count = len(result)
        with self.pool.cursor() as cur:
            cur.execute('SELECT id, username FROM users')
            users = [(None, self.gettext('All')), ('0', self.gettext('Orphan'))]
            for val, label in cur.fetchall():
                users.append((str(val), label))
            users_dict = dict(users)

        result = [dict(zip(FIELDS, x)) for x in result]
        for row in result:
            row['last_update'] = format_date(row['last_update'])
            row['flag_date'] = ('' if row['flag_date'] is None else
                                format_date(row['flag_date']))
            row['maintainer'] = users_dict.get(str(row['owner'] or '0'))
        parts = cherrypy.request.query_string.split('&')
        pager = '&'.join([x for x in parts if not x.startswith('page=')] + ['page='])
        sorter = '&'.join([x for x in parts if not x.startswith('sort=')] + ['sort='])
        tmpl = self._env.get_template('packages.html')
        return tmpl.render(
            packages=result, userinfo=userinfo, q=q, arch=arch,
            last_update=last_update, flagged=flagged, page=page, count=count,
            all_pages=all_pages, limit=limit, all_limits=AVAILABLE_LIMITS,
            pager=pager, sorter=sorter, maintainer=maintainer, users=users,
            all_arch=('any', 'i686', 'x86_64'))

    @cherrypy.expose
    def index(self):
        raise cherrypy.InternalRedirect('/query',
                                        query_string='sort=last_update')

    @cherrypy.expose
    def login(self, redirect_url=None):
        if not redirect_url:
            redirect_url = cherrypy.request.headers.get('REFERER', None)
        if not self.auth.getUserInfo():
            try:
                self.auth.login()
            except AuthError:
                tmpl = self._env.get_template('login.html')
                return tmpl.render(
                    flux_url=config.get('flux-sso', 'login-url'),
                    redirect_url=redirect_url)
            else:
                logging.debug('Logged in successfully')
        raise cherrypy.HTTPRedirect(redirect_url or '/')

    @cherrypy.expose
    def ajax_login(self):
        if not self.auth.getUserInfo():
            try:
                self.auth.login()
            except AuthError:
                raise cherrypy.HTTPError()

    @cherrypy.expose
    def logout(self, redirect_url=None):
        if not redirect_url:
            redirect_url = cherrypy.request.headers.get('REFERER', None)
        if self.auth.getUserInfo():
            self.auth.logout()
        if redirect_url:
            raise cherrypy.HTTPRedirect(redirect_url)


class ArchRepoWebServer(WSGIServer):
    def __init__(self, pool):
        host = config.xget('web', 'host', default='*')
        port = config.xgetint('web', 'port', default=8080)
        super(ArchRepoWebServer, self).__init__('%s:%d' % (host, port))
        cherrypy.server.unsubscribe()
        self.application = cherrypy.tree.mount(
            ArchRepoApplication(pool), config={'/': {'tools.sessions.on': True}})
