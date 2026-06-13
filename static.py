import datetime
import hashlib
import mimetypes
import os

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import db
from google.appengine.ext import deferred
from google.appengine.datastore import entity_pb
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app

import aetycoon
import config
import utils


HTTP_DATE_FMT = "%a, %d %b %Y %H:%M:%S GMT"

if config.google_site_verification is not None:
    ROOT_ONLY_FILES = ['/robots.txt','/' + config.google_site_verification]
else:
    ROOT_ONLY_FILES = ['/robots.txt']

class StaticContent(db.Model):
  """Container for statically served content.

  The serving path for content is provided in the key name.
  """
  body = db.BlobProperty()
  content_type = db.StringProperty()
  status = db.IntegerProperty(required=True, default=200)
  last_modified = db.DateTimeProperty(required=True)
  etag = aetycoon.DerivedProperty(lambda x: hashlib.sha1(x.body).hexdigest())
  indexed = db.BooleanProperty(required=True, default=True)
  headers = db.StringListProperty()


def get(path):
  """Returns the StaticContent object for the provided path.

  Args:
    path: The path to retrieve StaticContent for.
  Returns:
    A StaticContent object, or None if no content exists for this path.
  """
  entity = memcache.get(path)
  if entity:
    entity = db.model_from_protobuf(entity_pb.EntityProto(entity))
  else:
    entity = StaticContent.get_by_key_name(path)
    if entity:
      memcache.set(path, db.model_to_protobuf(entity).Encode())

  return entity


def set(path, body, content_type, indexed=True, **kwargs):
  """Sets the StaticContent for the provided path.

  Args:
    path: The path to store the content against.
    body: The data to serve for that path.
    content_type: The MIME type to serve the content as.
    indexed: Index this page in the sitemap?
    **kwargs: Additional arguments to be passed to the StaticContent constructor
  Returns:
    A StaticContent object.
  """
  now = datetime.datetime.now().replace(second=0, microsecond=0)
  defaults = {
    "last_modified": now,
  }
  defaults.update(kwargs)
  content = StaticContent(
      key_name=path,
      body=str(body),
      content_type=content_type,
      indexed=indexed,
      **defaults)
  content.put()
  memcache.replace(path, db.model_to_protobuf(content).Encode())
  try:
    eta = now.replace(second=0, microsecond=0) + datetime.timedelta(seconds=65)
    if indexed:
      deferred.defer(
          utils._regenerate_sitemap,
          _name='sitemap-%s' % (now.strftime('%Y%m%d%H%M'),),
          _eta=eta)
  except (taskqueue.taskqueue.TaskAlreadyExistsError, taskqueue.taskqueue.TombstonedTaskError), e:
    pass
  return content

def add(path, body, content_type, indexed=True, **kwargs):
  """Adds a new StaticContent and returns it.

  Args:
    As per set().
  Returns:
    A StaticContent object, or None if one already exists at the given path.
  """
  def _tx():
    if StaticContent.get_by_key_name(path):
      return None
    return set(path, body, content_type, indexed, **kwargs)
  return db.run_in_transaction(_tx)

def remove(path):
  """Deletes a StaticContent.

  Args:
    path: Path of the static content to be removed.
  """
  memcache.delete(path)
  def _tx():
    content = StaticContent.get_by_key_name(path)
    if not content:
      return
    content.delete()
  return db.run_in_transaction(_tx)

class StaticContentHandler(webapp.RequestHandler):
  def output_content(self, content, serve=True):
    if content.content_type:
      self.response.headers['Content-Type'] = content.content_type
    last_modified = content.last_modified.strftime(HTTP_DATE_FMT)
    self.response.headers['Last-Modified'] = last_modified
    self.response.headers['ETag'] = '"%s"' % (content.etag,)
    for header in content.headers:
      key, value = header.split(':', 1)
      self.response.headers[key] = value.strip()
    if serve:
      self.response.set_status(content.status)
      self.response.out.write(content.body)
    else:
      self.response.set_status(304)

  def get(self, path):
    # Resolve the internal lookup path from the request path.
    prefix = config.url_prefix
    if prefix and (path == prefix or path.startswith(prefix + '/')):
      # Path is under the url_prefix — strip it to get the storage key.
      lookup_path = path[len(prefix):]
      if not lookup_path:
        lookup_path = '/'
      # ROOT_ONLY_FILES must not be served under the prefix.
      if lookup_path in ROOT_ONLY_FILES:
        self.error(404)
        self.response.out.write(utils.render_template('404.html'))
        return
    else:
      # Path is at the root (or doesn't match the prefix).
      lookup_path = path

    content = get(lookup_path)

    # Static file fallback: if the datastore has no content and the path
    # looks like a theme static asset, serve directly from the filesystem.
    # This handles the case where url_prefix causes /static/... URLs to
    # bypass app.yaml's static file handler and fall through to this app.
    if not content:
      if self._try_serve_static_file(lookup_path):
        return

    if not content:
      self.error(404)
      self.response.out.write(utils.render_template('404.html'))
      return

    serve = True
    if 'If-Modified-Since' in self.request.headers:
      try:
        last_seen = datetime.datetime.strptime(
            self.request.headers['If-Modified-Since'].split(';')[0],# IE8 '; length=XXXX' as extra arg bug
            HTTP_DATE_FMT)
        if last_seen >= content.last_modified.replace(microsecond=0):
          serve = False
      except ValueError, e:
        import logging
        logging.error('StaticContentHandler in static.py, ValueError:' + self.request.headers['If-Modified-Since'])
    if 'If-None-Match' in self.request.headers:
      etags = [x.strip('" ')
               for x in self.request.headers['If-None-Match'].split(',')]
      if content.etag in etags:
        serve = False
    self.output_content(content, serve)

  def _try_serve_static_file(self, path):
    """Attempt to serve a theme static file from the filesystem.

    When url_prefix is set, template URLs like /static/theme/css/screen.css
    become /blog/static/theme/css/screen.css.  The app.yaml static handler
    only matches /static/... at the root, so prefixed requests fall through
    to this WSGI app.  This method serves them from the themes directory.

    Args:
      path: The un-prefixed request path.
    Returns:
      True if the file was served, False otherwise.
    """
    static_prefix = '/static/'
    if not path.startswith(static_prefix):
      return False

    remainder = path[len(static_prefix):]
    parts = remainder.split('/', 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
      return False

    theme_name, file_path = parts

    # Validate theme name: only alphanumeric, hyphens, underscores.
    if not all(c.isalnum() or c in '_-' for c in theme_name):
      return False

    # Resolve the filesystem path with traversal protection.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    theme_static_dir = os.path.normpath(
        os.path.join(base_dir, 'themes', theme_name, 'static'))
    full_path = os.path.normpath(
        os.path.join(theme_static_dir, file_path))

    # Ensure the resolved path stays within the theme's static directory.
    if not full_path.startswith(theme_static_dir + os.sep):
      return False

    if not os.path.isfile(full_path):
      return False

    content_type, _ = mimetypes.guess_type(full_path)
    if content_type is None:
      content_type = 'application/octet-stream'

    with open(full_path, 'rb') as f:
      body = f.read()

    self.response.headers['Content-Type'] = content_type
    self.response.out.write(body)
    return True


application = webapp.WSGIApplication([
                ('(/.*)', StaticContentHandler),
              ])


def main():
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
