import glob
from babel.messages import frontend as babel
from distutils.core import setup


setup(
    name='ArchRepo',
    version='1.0a1',
    author='Fantix King',
    author_email='fantix.king@gmail.com',
    packages=['archrepo'],
    scripts=['bin/archrepo_inotify.py',
             'bin/read_pkginfo.py',
             'bin/archrepo_serve.py',
             ],
    package_data={'archrepo': ['templates/*']},
    data_files=[
        ('etc', ['archrepo.ini']),
        ('locale', glob.glob('locale/*/*/*.po')),
    ],
    url='http://www.archlinuxcn.org',
    license='LICENSE.txt',
    description='Package management for archlinuxcn.org',
    long_description=open('README.md').read(),
    install_requires=[
        "gevent==1.0b3", "pyinotify", "ujson", "cherrypy", "jinja2", "psycopg2"
        ],
    cmdclass = {'compile_catalog': babel.compile_catalog,
                'extract_messages': babel.extract_messages,
                'init_catalog': babel.init_catalog,
                'update_catalog': babel.update_catalog}
    )
