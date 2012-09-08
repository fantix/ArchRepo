import glob
from babel.messages import frontend as babel
from distutils.command import build
from distutils.core import setup


class my_build(build.build):
    sub_commands = build.build.sub_commands + [('compile_catalog', lambda *x: True)]


class my_compile_catalog(babel.compile_catalog):
    def initialize_options(self):
        babel.compile_catalog.initialize_options(self)
        self.directory = 'locale'
        self.domain = 'archrepo'


data_files = {'etc': ['archrepo.ini']}
for fn in glob.glob('locale/*/*/*.po'):
    path, _ = fn.rsplit('/', 1)
    fn = fn[:-3] + '.mo'
    data_files.setdefault('share/' + path, []).append(fn)

setup(
    name='ArchRepo',
    version='1.0b1',
    author='Fantix King',
    author_email='fantix.king@gmail.com',
    packages=['archrepo'],
    scripts=['bin/archrepo_inotify.py',
             'bin/read_pkginfo.py',
             'bin/archrepo_serve.py',
             'bin/archrepo_sync.py',
             ],
    package_data={'archrepo': ['templates/*']},
    data_files=list(data_files.iteritems()),
    url='http://www.archlinuxcn.org',
    license='LICENSE.txt',
    description='Package management for archlinuxcn.org',
    long_description=open('README.md').read(),
    install_requires=[
        "gevent==1.0b3", "pyinotify", "ujson", "cherrypy", "jinja2", "psycopg2",
        "babel", "pyliblzma", "gevent_zeromq"
        ],
    cmdclass = {
        'build': my_build,
        'compile_catalog': my_compile_catalog,
        'extract_messages': babel.extract_messages,
        'init_catalog': babel.init_catalog,
        'update_catalog': babel.update_catalog}
    )
