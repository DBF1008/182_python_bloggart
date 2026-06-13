import aetycoon
import datetime
import hashlib
import re
from google.appengine.ext import db
from google.appengine.ext import deferred

import config
import generators
import markup
import page_logic
import static
import utils


if config.default_markup in markup.MARKUP_MAP:
  DEFAULT_MARKUP = config.default_markup
else:
  DEFAULT_MARKUP = 'html'


class BlogDate(db.Model):
  """Contains a list of year-months for published blog posts."""

  @classmethod
  def get_key_name(cls, post):
    return '%d/%02d' % (post.published_tz.year, post.published_tz.month)

  @classmethod
  def create_for_post(cls, post):
    inst = BlogDate(key_name=BlogDate.get_key_name(post))
    inst.put()
    return inst

  @classmethod
  def datetime_from_key_name(cls, key_name):
    year, month = key_name.split("/")
    return datetime.datetime(int(year), int(month), 1, tzinfo=utils.tzinfo())

  @property
  def date(self):
    return BlogDate.datetime_from_key_name(self.key().name()).date()


class BlogPost(db.Model):
  # The URL path to the blog post. Posts have a path iff they are published.
  path = db.StringProperty()
  title = db.StringProperty(required=True, indexed=False)
  body_markup = db.StringProperty(choices=set(markup.MARKUP_MAP),
                                  default=DEFAULT_MARKUP)
  body = db.TextProperty(required=True)
  tags = aetycoon.SetProperty(basestring, indexed=False)
  published = db.DateTimeProperty()
  updated = db.DateTimeProperty(auto_now=False)
  deps = aetycoon.PickleProperty()

  @property
  def published_tz(self):
    return utils.tz_field(self.published)

  @property
  def updated_tz(self):
    return utils.tz_field(self.updated)

  @aetycoon.TransformProperty(tags)
  def normalized_tags(tags):
    return list(set(utils.slugify(x.lower()) for x in tags))

  @property
  def tag_pairs(self):
    return [(x, utils.slugify(x.lower())) for x in self.tags]

  @property
  def rendered(self):
    """Returns the rendered body."""
    return markup.render_body(self)

  @property
  def summary(self):
    """Returns a summary of the blog post."""
    return markup.render_summary(self)

  @property
  def hash(self):
    val = (self.title, self.body, self.published)
    return hashlib.sha1(str(val)).hexdigest()

  @property
  def summary_hash(self):
    val = (self.title, self.summary, self.tags, self.published)
    return hashlib.sha1(str(val)).hexdigest()

  def publish(self):
    regenerate = False
    if not self.path:
      num = 0
      content = None
      while not content:
        path = utils.format_post_path(self, num)
        content = static.add(path, '', config.html_mime_type)
        num += 1
      self.path = path
      self.put()
      # Force regenerate on new publish. Also helps with generation of
      # chronologically previous and next page.
      regenerate = True

    BlogDate.create_for_post(self)

    for generator_class, deps in self.get_deps(regenerate=regenerate):
      for dep in deps:
        if generator_class.can_defer:
          deferred.defer(generator_class.generate_resource, None, dep)
        else:
          generator_class.generate_resource(self, dep)
    self.put()

  def remove(self):
    if not self.is_saved():
      return
    # It is important that the get_deps() return the post dependency
    # before the list dependencies as the BlogPost entity gets deleted
    # while calling PostContentGenerator.
    for generator_class, deps in self.get_deps(regenerate=True):
      for dep in deps:
        if generator_class.can_defer:
          deferred.defer(generator_class.generate_resource, None, dep)
        else:
          if generator_class.name() == 'PostContentGenerator':
            generator_class.generate_resource(self, dep, action='delete')
            self.delete()
          else:
            generator_class.generate_resource(self, dep)

  def get_deps(self, regenerate=False):
    if not self.deps:
      self.deps = {}
    for generator_class in generators.generator_list:
      new_deps = set(generator_class.get_resource_list(self))
      new_etag = generator_class.get_etag(self)
      old_deps, old_etag = self.deps.get(generator_class.name(), (set(), None))
      if new_etag != old_etag or regenerate:
        # If the etag has changed, regenerate everything
        to_regenerate = new_deps | old_deps
      else:
        # Otherwise just regenerate the changes
        to_regenerate = new_deps ^ old_deps
      self.deps[generator_class.name()] = (new_deps, new_etag)
      yield generator_class, to_regenerate

class Page(db.Model):
  # The URL path to the page.
  path = db.StringProperty(required=True)
  title = db.TextProperty(required=True)
  template = db.StringProperty(required=True)
  body = db.TextProperty(required=True)
  created = db.DateTimeProperty(required=True, auto_now_add=True)
  updated = db.DateTimeProperty()

  @property
  def rendered(self):
    # Returns the rendered body.
    return markup.render_body(self)

  @property
  def hash(self):
    val = (self.path, self.title, self.body, self.updated)
    return hashlib.sha1(str(val)).hexdigest()

  def publish(self):
    # Only a brand new (unsaved) entity may have its key_name assigned; an
    # existing entity's key is immutable, so a path change goes through
    # save_page(), which creates a fresh entity at the new path.
    if not self.is_saved():
      self._key_name = self.path
    self.put()
    generators.PageContentGenerator.generate_resource(self, self.path)

  def remove(self):
    if not self.is_saved():
      return
    self.delete()
    generators.PageContentGenerator.generate_resource(self, self.path, action='delete')

  @classmethod
  def save_page(cls, original_path, path, title, template, body):
    """Creates, edits, or renames a page; the single entry point for saves.

    ``original_path`` is the path of the page being edited, or None when
    creating a new page. Returns the live Page. Raises
    ``page_logic.PageConflictError`` when ``path`` is already owned by a
    different page.

    Every create / edit-in-place / rename / collision decision is made by
    ``page_logic.resolve_page_save`` so this stays the only code that mutates
    pages, keeping the change-path behaviour consistent.
    """
    path = page_logic.validate_path(path)
    if original_path is not None:
      original_path = page_logic.normalize_path(original_path)
    plan = page_logic.resolve_page_save(
        original_path, path,
        lambda p: cls.get_by_key_name(p) is not None)

    page = None
    if not plan.is_rename and original_path is not None:
      # Edit in place: reuse the entity that already lives at this key_name.
      page = cls.get_by_key_name(path)
      if page is not None:
        page.title = title
        page.template = template
        page.body = body
    if page is None:
      # New page, or rename (a new key_name requires a new entity).
      page = cls(key_name=path, path=path, title=title, template=template,
                 body=body)
      if plan.is_rename:
        original = cls.get_by_key_name(original_path)
        if original is not None and original.created is not None:
          page.created = original.created  # preserve creation date on rename

    page.updated = datetime.datetime.now()
    page.publish()

    # Publish the new content first, then remove the old path's entity and its
    # static page, so a rename never leaves a duplicate or a dangling page.
    if plan.old_path_to_remove is not None:
      old = cls.get_by_key_name(plan.old_path_to_remove)
      if old is not None:
        old.remove()
    return page

class VersionInfo(db.Model):
  bloggart_major = db.IntegerProperty(required=True)
  bloggart_minor = db.IntegerProperty(required=True)
  bloggart_rev = db.IntegerProperty(required=True)

  @property
  def bloggart_version(self):
    return (self.bloggart_major, self.bloggart_minor, self.bloggart_rev)
