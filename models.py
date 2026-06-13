import aetycoon
import datetime
import hashlib
import re
from google.appengine.ext import db
from google.appengine.ext import deferred

import config
import generators
import markup
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

  def publish(self, force=False):
    regenerate = force
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
    self._clear_draft()

  def _clear_draft(self):
    """Removes the draft preview page (if any) for this post's title.

    Publishing from any entry point clears the matching /draft/<slug> page, so
    the command-line script and the admin leave the site in the same state.
    Uses static.remove() so the memcache entry is cleared too; a no-op when no
    draft exists.
    """
    static.remove('/draft/' + utils.slugify(self.title))

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


class PathExistsError(Exception):
  """Raised when a published post already owns the target path.

  publish_post() raises this on a path collision when force is not set, instead
  of silently overwriting an existing post.
  """


def publish_post(title, body, tags, force=False):
  """Creates or force-overwrites a published BlogPost from raw fields.

  This is the single write/cleanup entry point shared by the command-line
  publish script and (via BlogPost.publish) the admin handler, so static
  content, caching, draft cleanup and dependent-page regeneration are handled
  identically regardless of where the publish originates.

  Args:
    title: The post title.
    body: The (already-extracted) post body markup.
    tags: An iterable of tag strings.
    force: If True, overwrite an existing post at the same path, fully
      regenerating its static content and dependent pages. If False and a post
      already owns the path, PathExistsError is raised.
  Returns:
    The published BlogPost entity.
  """
  now = datetime.datetime.now()
  post = BlogPost(title=title, body=body, tags=tags, published=now, updated=now)
  path = utils.format_post_path(post, 0)
  existing = BlogPost.all().filter('path =', path).get()
  if existing:
    if not force:
      raise PathExistsError(path)
    existing.title = title
    existing.body = body
    existing.tags = tags
    existing.updated = now
    existing.publish(force=True)
    return existing
  # No published post owns this path: a normal first-time publish, which
  # allocates the path (with collision suffixes) and regenerates everything.
  post.publish()
  return post


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
    val = (self.path, self.body, self.published)
    return hashlib.sha1(str(val)).hexdigest()

  def publish(self):
    self._key_name = self.path
    self.put()
    generators.PageContentGenerator.generate_resource(self, self.path);

  def remove(self):
    if not self.is_saved():   
      return
    self.delete()
    generators.PageContentGenerator.generate_resource(self, self.path, action='delete')

class VersionInfo(db.Model):
  bloggart_major = db.IntegerProperty(required=True)
  bloggart_minor = db.IntegerProperty(required=True)
  bloggart_rev = db.IntegerProperty(required=True)

  @property
  def bloggart_version(self):
    return (self.bloggart_major, self.bloggart_minor, self.bloggart_rev)
