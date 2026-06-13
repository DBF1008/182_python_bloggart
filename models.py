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
  def get_key_name_for_published(cls, published):
    """Returns the year/month key name for a raw published datetime."""
    date = utils.tz_field(published)
    return '%d/%02d' % (date.year, date.month)

  @classmethod
  def get_key_name(cls, post):
    return cls.get_key_name_for_published(post.published)

  @classmethod
  def create_for_post(cls, post):
    inst = BlogDate(key_name=BlogDate.get_key_name(post))
    inst.put()
    return inst

  @classmethod
  def remove_if_empty(cls, published):
    """Deletes the BlogDate for published's month if no posts remain in it.

    Editing a post's publish date can move it to a different month. When that
    leaves the old month with no posts, its BlogDate would otherwise linger and
    keep showing up as a phantom entry in the archive index, so we drop it.

    The caller is responsible for persisting the post's new publish date first,
    so the emptiness check below does not still see the moved post.
    """
    key_name = cls.get_key_name_for_published(published)
    blogdate = cls.get_by_key_name(key_name)
    if not blogdate:
      return
    ts = cls.datetime_from_key_name(key_name)
    min_ts = ts.replace(day=1)
    # Python doesn't wrap the month for us, so handle December manually.
    if min_ts.month >= 12:
      max_ts = min_ts.replace(year=min_ts.year + 1, month=1)
    else:
      max_ts = min_ts.replace(month=min_ts.month + 1)
    q = BlogPost.all(keys_only=True)
    q.filter('published >=', min_ts)
    q.filter('published <', max_ts)
    if q.get() is None:
      blogdate.delete()

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

  def _path_is_current(self):
    """Returns True if self.path still matches the title slug and publish month.

    The path is derived from the title (via the slug) and the published
    year/month, so a change to either invalidates the existing path and the
    post must be moved to a fresh one.
    """
    if not self.path:
      return False
    base = utils.format_post_path(self, 0)
    if self.path == base:
      return True
    # The path may carry a "-1", "-2", ... suffix that was appended to avoid a
    # slug clash with another post. Such a path is still current for this
    # title/month, so treat base + numeric suffix as a match.
    prefix = base + '-'
    suffix = self.path[len(prefix):]
    return self.path.startswith(prefix) and suffix.isdigit()

  def _reserve_path(self):
    """Reserves and returns an unused path for the current title and month.

    Appends "-1", "-2", ... when the preferred path is already taken by a
    different post (duplicate slug). The post's own current path is never a
    candidate here, because this is only called once self.path is known to be
    stale, so it can never collide with itself.
    """
    num = 0
    while True:
      path = utils.format_post_path(self, num)
      if static.add(path, '', config.html_mime_type):
        return path
      num += 1

  def publish(self):
    # Remember the month this post was previously filed under (its persisted
    # publish date) so we can tidy up the archive if this edit moves it.
    prev = None
    old_published = None
    if self.is_saved():
      prev = db.get(self.key())
      if prev:
        old_published = prev.published

    regenerate = False
    old_path = None
    if not self._path_is_current():
      # New post, or the title/month changed: move to a fresh path and retire
      # the stale one. Forcing a regenerate makes every listing and the
      # chronological neighbours pick up the new URL.
      old_path = self.path
      self.path = self._reserve_path()
      regenerate = True
    elif (prev is None
          or prev.title != self.title
          or prev.body != self.body
          or prev.published != self.published
          or set(prev.tags) != set(self.tags)):
      # The path stays the same, but a field that affects rendered output
      # changed. Regenerate this post's resources (its page, the listings it
      # appears on, the feed, and its neighbours) so nothing goes stale.
      regenerate = True

    # Persist the new state *before* (re)generating dependent resources. The
    # deferrable generators below re-read this post from the datastore, so they
    # must observe the new path, title, tags and publish date.
    self.put()
    if old_path and old_path != self.path:
      static.remove(old_path)

    BlogDate.create_for_post(self)
    # If this edit moved the post into a different month, drop the previous
    # month's archive entry when nothing else is filed there.
    if (old_published is not None
        and old_published != datetime.datetime.max
        and BlogDate.get_key_name_for_published(old_published)
            != BlogDate.get_key_name(self)):
      BlogDate.remove_if_empty(old_published)

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
