import os

from archrepo import config


schema = {
    'packages': '''\
CREATE TABLE packages (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    arch        text NOT NULL,
    version     text NOT NULL,
    description text,
    url         text,
    pkg_group   text,
    license     text,
    packager    text,
    base_name   text,
    file_path   text NOT NULL,
    build_date  integer,
    size        integer,
    depends     text[],
    opt_depends text[],
    enabled     boolean NOT NULL DEFAULT true,
    latest      boolean NOT NULL DEFAULT false,
    last_update timestamp without time zone NOT NULL DEFAULT now(),
    searchable  tsvector NOT NULL DEFAULT to_tsvector('english', '')
);
CREATE INDEX package_by_name ON packages (name);
CREATE INDEX package_by_path ON packages (file_path);
CREATE INDEX package_by_latest ON packages (latest);
CREATE INDEX package_by_name_arch_enabled ON packages (name, arch, enabled);
CREATE INDEX package_by_name_arch_latest ON packages (name, arch, latest);
CREATE UNIQUE INDEX package_by_name_arch_ver ON packages (name, arch, version);
CREATE INDEX package_by_description ON packages USING gin(searchable) WHERE latest;

CREATE TRIGGER update_packages_searchable BEFORE INSERT OR UPDATE
    ON packages FOR EACH ROW EXECUTE PROCEDURE
    tsvector_update_trigger(searchable, 'pg_catalog.english', name, base_name, description);
''',
    'users': '''\
CREATE TABLE users (
    id          bigint PRIMARY KEY,
    username    text,
    email       text,
    title       text,
    realname    text
);
'''
}


def initSchema(pool):
    with pool.cursor() as cur:
        cur.execute(
            'SELECT tablename FROM pg_tables '
             'WHERE tableowner=%s',
            (config.xget('database', 'user', default=os.getlogin()),))
        result = set([x[0] for x in cur.fetchall()])
        for key in schema:
            if key not in result:
                cur.execute(schema[key])
