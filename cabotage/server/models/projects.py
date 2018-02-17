import json

from citext import CIText
from sqlalchemy import text, UniqueConstraint
from sqlalchemy.event import listens_for
from sqlalchemy.dialects import postgresql
from sqlalchemy_continuum import make_versioned
from sqlalchemy_utils.models import Timestamp

from cabotage.server import db

from cabotage.server.models.plugins import ActivityPlugin
from cabotage.server.models.utils import (
    slugify,
    DictDiffer,
)

activity_plugin = ActivityPlugin()
make_versioned(plugins=[activity_plugin])

platform_version = postgresql.ENUM(
    'wind',
    'steam',
    'diesel',
    'stirling',
    'nuclear',
    'electric',
    name='platform_version',
)


class Project(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'projects'

    def __init__(self, *args, **kwargs):
        if 'slug' not in kwargs:
            kwargs['slug'] = slugify(kwargs.get('name'))
        super().__init__(*args, **kwargs)

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    organization_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('organizations.id'),
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)

    project_applications = db.relationship(
        "Application",
        backref="project",
        cascade="all, delete-orphan",
    )

    UniqueConstraint(organization_id, slug)


class Application(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_applications'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    project_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('projects.id'),
        nullable=False,
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)
    platform = db.Column(platform_version, nullable=False, default='wind')

    images = db.relationship(
        "Image",
        backref="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    configurations = db.relationship(
        "Configuration",
        backref="application",
        cascade="all, delete-orphan",
    )
    releases = db.relationship(
        "Release",
        backref="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    version_id = db.Column(
        db.Integer,
        nullable=False
    )

    @property
    def release_candidate(self):
        release = Release(
            application_id=self.id,
            image=self.latest_image.asdict if self.latest_image else {},
            configuration={c.name: c.asdict for c in self.configurations},
            platform=self.platform,
        )
        return release.asdict

    @property
    def latest_release(self):
        return self.releases.filter_by().order_by(Release.version.desc()).first()

    @property
    def latest_release_built(self):
        return self.releases.filter_by(built=True).order_by(Release.version.desc()).first()

    @property
    def latest_release_error(self):
        return self.releases.filter_by(error=True).order_by(Release.version.desc()).first()

    @property
    def latest_release_building(self):
        return self.releases.filter_by(built=False, error=False).order_by(Release.version.desc()).first()

    @property
    def current_release(self):
        if self.latest_release:
            return self.latest_release.asdict
        return {}

    @property
    def ready_for_deployment(self):
        current = self.current_release
        candidate = self.release_candidate
        configuration_diff = DictDiffer(
            candidate.get('configuration', {}),
            current.get('configuration', {}),
            ignored_keys=['id', 'version_id'],
        )
        image_diff = DictDiffer(
            candidate.get('image', {}),
            current.get('image', {}),
            ignored_keys=['id', 'version_id'],
        )
        return image_diff, configuration_diff

    def create_release(self):
        image_diff, configuration_diff = self.ready_for_deployment
        release = Release(
            application_id=self.id,
            image=self.latest_image.asdict,
            configuration={c.name: c.asdict for c in self.configurations},
            image_changes=image_diff.asdict,
            configuration_changes=configuration_diff.asdict,
            platform=self.platform,
        )
        return release

    @property
    def latest_image(self):
        return self.images.filter_by(built=True).order_by(Image.version.desc()).first()

    @property
    def latest_image_error(self):
        return self.images.filter_by(error=True).order_by(Image.version.desc()).first()

    @property
    def latest_image_building(self):
        return self.images.filter_by(built=False, error=False).order_by(Image.version.desc()).first()

    UniqueConstraint(project_id, slug)

    __mapper_args__ = {
        "version_id_col": version_id
    }


class Release(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_app_releases'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('project_applications.id'),
        nullable=False,
    )
    platform = db.Column(platform_version, nullable=False, default='wind')
    image = db.Column(postgresql.JSONB(), nullable=False)
    configuration = db.Column(postgresql.JSONB(), nullable=False)
    image_changes = db.Column(postgresql.JSONB(), nullable=False)
    configuration_changes = db.Column(postgresql.JSONB(), nullable=False)
    version_id = db.Column(
        db.Integer,
        nullable=False
    )

    version = db.Column(
        db.Integer,
        nullable=False,
    )

    built = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    error = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    error_detail = db.Column(
        db.String(2048),
        nullable=True,
    )
    deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    release_metadata = db.Column(
        postgresql.JSONB(),
        nullable=True,
    )
    release_build_log = db.Column(
        db.Text(),
        nullable=True,
    )

    __mapper_args__ = {
        "version_id_col": version_id
    }

    @property
    def valid(self):
        return (
            (self.image_object is not None)
            and
            all(v is not None for v in self.configuration_objects.values())
        )

    @property
    def deposed(self):
        return not self.valid

    @property
    def deposed_reason(self):
        reasons = []
        if self.image_object is None:
            reasons.append(f'<code>Image({self.image["id"]})</code> no longer exists!')
        for configuration, configuration_serialized in self.configuration.items():
            configuration_object = Configuration.query.filter_by(id=configuration_serialized["id"]).first()
            if configuration_object is None:
                reasons.append(f'<code>Configuration({configuration_serialized["id"]})</code> for <code>{configuration}</code> no longer exists!')
        return reasons

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "application_id": str(self.application_id),
            "platform": self.platform,
            "image": self.image,
            "configuration": self.configuration,
        }

    @property
    def configuration_objects(self):
        return {
            k: Configuration.query.filter_by(id=v["id"]).first()
            for k, v in self.configuration.items()
        }

    @property
    def envconsul_configurations(self):
        configurations = {}
        environment_statements = '\n'.join([
            c.envconsul_statement for c in self.configuration_objects.values()
            if c is not None
        ])
        for proc_name, proc in self.image_object.processes.items():
            custom_env = json.dumps([f"{key}={value}" for key, value in proc['env']])
            exec_statement = (
                 'exec {\n'
                f'  command = {json.dumps(proc["cmd"])}\n'
                 '  env = {\n'
                 '    pristine = true\n'
                f'    custom = {custom_env}\n'
                 '  }\n'
                 '}'
            )
            configurations[proc_name] = '\n'.join([exec_statement, environment_statements])
        return configurations

    @property
    def image_object(self):
        return Image.query.filter_by(id=self.image["id"]).first()


@listens_for(Release, 'before_insert')
def release_before_insert_listener(mapper, connection, target):
    most_recent_release = mapper.class_.query.filter_by(application_id=target.application_id).order_by(mapper.class_.version.desc()).first()
    if most_recent_release is None:
        target.version = 1
    else:
        target.version = most_recent_release.version + 1

class Configuration(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_app_configurations'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('project_applications.id'),
        nullable=False,
    )

    name = db.Column(
        CIText(),
        nullable=False,
    )
    value = db.Column(
        db.String(2048),
        nullable=False,
    )
    key_slug = db.Column(
        db.Text(),
        nullable=True,
    )
    version_id = db.Column(
        db.Integer,
        nullable=False
    )
    deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    secret = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    UniqueConstraint(application_id, name)

    __mapper_args__ = {
        "version_id_col": version_id
    }

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "version_id": self.version_id,
            "secret": self.secret,
        }

    @property
    def envconsul_statement(self):
        directive = 'secret' if self.secret else 'prefix'
        path = self.key_slug.split(':', 1)[1]
        return (
            f'{directive} {{\n'
             '  no_prefix = true\n'
            f'  path = "{path}"\n'
             '}'
        )


class Image(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_app_images'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('project_applications.id'),
        nullable=False,
    )

    repository_name = db.Column(
        db.String(256),
        nullable=False,
    )
    image_id = db.Column(
        db.String(256),
        nullable=True,
    )
    version = db.Column(
        db.Integer,
        nullable=False,
    )

    version_id = db.Column(
        db.Integer,
        nullable=False,
    )
    built = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    error = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    error_detail = db.Column(
        db.String(2048),
        nullable=True,
    )
    deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    build_slug = db.Column(
        db.String(1024),
        nullable=False,
    )
    dockerfile = db.Column(
        db.Text(),
        nullable=True,
    )
    procfile = db.Column(
        db.Text(),
        nullable=True,
    )
    processes = db.Column(
        postgresql.JSONB(),
        nullable=True,
    )
    image_metadata = db.Column(
        postgresql.JSONB(),
        nullable=True,
    )
    image_build_log = db.Column(
        db.Text(),
        nullable=True,
    )

    __mapper_args__ = {
        "version_id_col": version_id
    }

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "repository": self.repository_name,
            "tag": str(self.version),
            "processes": self.processes,
        }


@listens_for(Image, 'before_insert')
def image_before_insert_listener(mapper, connection, target):
    most_recent_image = mapper.class_.query.filter_by(application_id=target.application_id).order_by(mapper.class_.version.desc()).first()
    if most_recent_image is None:
        target.version = 1
    else:
        target.version = most_recent_image.version + 1
