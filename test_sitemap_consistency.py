"""Regression tests for static-content deletion and sitemap/index updates.

These tests pin down the fix that keeps the sitemap -- and its gzipped copy --
consistent with the set of actually-served, indexed pages after a post or page
is deleted or has its access path changed. Before the fix, ``static.remove()``
deleted the served content but never scheduled a sitemap regeneration, so the
sitemap kept advertising URLs that no longer resolved.

Bloggart targets Python 2.7 / Google App Engine, and the App Engine SDK is not
available in this environment, so the handful of infrastructure dependencies of
``static.py`` / ``utils.py`` (the datastore, memcache, the deferred task queue,
``aetycoon`` and ``django``) are replaced with small in-memory fakes installed
into ``sys.modules`` *before* the modules under test are imported. Everything
that the fix touches -- ``static.set`` / ``static.remove`` /
``static._schedule_sitemap_regeneration`` and ``utils._get_all_paths`` /
``utils._regenerate_sitemap`` -- runs for real against those fakes.
"""

import io
import os
import sys
import types
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Make the application modules importable from the repository root.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# In-memory fakes for the App Engine / third-party dependencies.
# ---------------------------------------------------------------------------

def _new_module(fullname):
  """Creates an empty module, registers it, and attaches it to its parent."""
  module = types.ModuleType(fullname)
  sys.modules[fullname] = module
  if '.' in fullname:
    parent_name, child_name = fullname.rsplit('.', 1)
    setattr(sys.modules[parent_name], child_name, module)
  return module


# --- datastore (google.appengine.ext.db) -----------------------------------

class _FakeKey(object):
  def __init__(self, name):
    self._name = name

  def name(self):
    return self._name


def _compare(left, op, right):
  if op == '>':
    return left > right
  if op == '>=':
    return left >= right
  if op == '<':
    return left < right
  if op == '<=':
    return left <= right
  return left == right


class _FakeQuery(object):
  """A tiny stand-in for db.Query supporting the calls bloggart makes."""

  def __init__(self, model_cls, keys_only=False):
    self._model_cls = model_cls
    self._keys_only = keys_only
    self._filters = []

  def filter(self, expression, value):
    self._filters.append((expression, value))
    return self

  def order(self, _prop):  # pragma: no cover - unused by the code under test
    return self

  def fetch(self, limit, offset=0):
    items = list(self._model_cls._entities().values())
    for expression, value in self._filters:
      parts = expression.split()
      prop = parts[0]
      op = parts[1] if len(parts) > 1 else '='
      if prop == '__key__':
        items = [e for e in items if _compare(e._key_name, op, value.name())]
      else:
        items = [e for e in items if _compare(getattr(e, prop, None), op, value)]
    items.sort(key=lambda e: e._key_name)
    items = items[offset:offset + limit]
    if self._keys_only:
      return [e.key() for e in items]
    return items


class _FakeModel(object):
  """Minimal db.Model: key-name addressed entities in a per-class dict."""

  def __init__(self, key_name=None, **kwargs):
    self._key_name = key_name
    for name, value in kwargs.items():
      setattr(self, name, value)

  @classmethod
  def _entities(cls):
    store = cls.__dict__.get('_store')
    if store is None:
      store = {}
      cls._store = store
    return store

  @classmethod
  def get_by_key_name(cls, key_name):
    return cls._entities().get(key_name)

  @classmethod
  def all(cls, keys_only=False):
    return _FakeQuery(cls, keys_only=keys_only)

  def put(self):
    self._entities()[self._key_name] = self
    return _FakeKey(self._key_name)

  def delete(self):
    self._entities().pop(self._key_name, None)

  def is_saved(self):
    return self._key_name in self._entities()

  def key(self):
    return _FakeKey(self._key_name)


def _passthrough_property(*_args, **_kwargs):
  """db.*Property() factory: the value lives on the instance, not the class."""
  return None


class _FakeProtobuf(object):
  def __init__(self, entity):
    self._entity = entity

  def Encode(self):
    return self._entity


# --- memcache (google.appengine.api.memcache) ------------------------------

_memcache_store = {}


def _mc_get(key, *_args, **_kwargs):
  return _memcache_store.get(key)


def _mc_set(key, value, *_args, **_kwargs):
  _memcache_store[key] = value
  return True


def _mc_replace(key, value, *_args, **_kwargs):
  if key in _memcache_store:
    _memcache_store[key] = value
    return True
  return False


def _mc_delete(key, *_args, **_kwargs):
  _memcache_store.pop(key, None)
  return True


# --- deferred / taskqueue --------------------------------------------------

class _FakeTaskAlreadyExistsError(Exception):
  pass


class _FakeTombstonedTaskError(Exception):
  pass


_deferred_tasks = []         # list of (fn, args, kwargs)
_deferred_used_names = set()  # task names that have already been enqueued


def _defer(fn, *args, **kwargs):
  name = kwargs.get('_name')
  if name is not None:
    if name in _deferred_used_names:
      # Mirror App Engine: a same-named task in the dedup window is rejected,
      # which is how bloggart coalesces a burst of changes into one rebuild.
      raise _FakeTaskAlreadyExistsError(name)
    _deferred_used_names.add(name)
  _deferred_tasks.append((fn, args, kwargs))


# ---------------------------------------------------------------------------
# Install the fake module tree (parents first).
# ---------------------------------------------------------------------------

_new_module('google')
_new_module('google.appengine')
_new_module('google.appengine.api')

_memcache = _new_module('google.appengine.api.memcache')
_memcache.get = _mc_get
_memcache.set = _mc_set
_memcache.replace = _mc_replace
_memcache.delete = _mc_delete

_taskqueue = _new_module('google.appengine.api.taskqueue')
_taskqueue_inner = types.SimpleNamespace(
    TaskAlreadyExistsError=_FakeTaskAlreadyExistsError,
    TombstonedTaskError=_FakeTombstonedTaskError)
_taskqueue.taskqueue = _taskqueue_inner  # static.py uses taskqueue.taskqueue.*
_taskqueue.TaskAlreadyExistsError = _FakeTaskAlreadyExistsError
_taskqueue.TombstonedTaskError = _FakeTombstonedTaskError

_new_module('google.appengine.datastore')
_entity_pb = _new_module('google.appengine.datastore.entity_pb')
_entity_pb.EntityProto = lambda payload: payload

_new_module('google.appengine.ext')

_db = _new_module('google.appengine.ext.db')
_db.Model = _FakeModel
_db.run_in_transaction = lambda fn, *a, **k: fn(*a, **k)
_db.model_to_protobuf = lambda entity: _FakeProtobuf(entity)
_db.model_from_protobuf = lambda payload: payload
for _name in ('BlobProperty', 'StringProperty', 'IntegerProperty',
              'DateTimeProperty', 'BooleanProperty', 'StringListProperty',
              'TextProperty'):
  setattr(_db, _name, _passthrough_property)

_deferred = _new_module('google.appengine.ext.deferred')
_deferred.defer = _defer

_webapp = _new_module('google.appengine.ext.webapp')
_webapp.RequestHandler = type('RequestHandler', (object,), {})
_webapp.WSGIApplication = lambda *a, **k: object()

_webapp_template = _new_module('google.appengine.ext.webapp.template')
_webapp_template.create_template_register = lambda: types.SimpleNamespace(
    filter=lambda *a, **k: None)
_webapp_template._swap_settings = lambda settings: {}
_webapp.template = _webapp_template

_webapp_util = _new_module('google.appengine.ext.webapp.util')
_webapp_util.run_wsgi_app = lambda app: None


# --- aetycoon (a git submodule, not checked out here) ----------------------

class _FakeDerivedProperty(object):
  def __init__(self, fn, *args, **kwargs):
    self._fn = fn

  def __get__(self, obj, objtype=None):
    if obj is None:
      return self
    return self._fn(obj)


_aetycoon = _new_module('aetycoon')
_aetycoon.DerivedProperty = _FakeDerivedProperty

_xsrfutil = _new_module('xsrfutil')
_xsrfutil.xsrf_token = lambda *a, **k: ''


# --- django (only what utils imports at module load) -----------------------

_django = _new_module('django')
_new_module('django.conf')
_django_template = _new_module('django.template')
_django_template.builtins = []
_django_template.Context = type('Context', (object,), {
    '__init__': lambda self, values=None: None})
_django_template_loader = _new_module('django.template.loader')
_django_template_loader.get_template = lambda name: types.SimpleNamespace(
    render=lambda ctx: '')


# ---------------------------------------------------------------------------
# Compatibility shims so the real utils._regenerate_sitemap (written for
# Python 2) can run under Python 3. These only stand in for stdlib pieces that
# are unrelated to the bug: StringIO (gone in py3) and gzip's bytes-only write.
# The shims faithfully pass content through so we can assert on it.
# ---------------------------------------------------------------------------

class _PassThroughGzipFile(object):
  def __init__(self, fileobj=None, mode=None, **_kwargs):
    self._fileobj = fileobj

  def write(self, data):
    self._fileobj.write(data)

  def close(self):
    pass

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False


_fake_gzip = types.ModuleType('gzip')
_fake_gzip.GzipFile = _PassThroughGzipFile

_fake_stringio = types.ModuleType('StringIO')
_fake_stringio.StringIO = io.StringIO


# ---------------------------------------------------------------------------
# Import the code under test (now that the fakes are in place).
# ---------------------------------------------------------------------------

import config   # noqa: E402  (real module; imported after fakes are installed)
import static   # noqa: E402
import utils    # noqa: E402

HTML = config.html_mime_type


def _fake_render_template(template_name, template_vals=None, theme=None):
  """Renders just enough of sitemap.xml to assert which URLs it contains."""
  if template_name == 'sitemap.xml':
    locs = ''.join('<url><loc>%s</loc></url>' % p
                   for p in template_vals['paths'])
    return '<urlset>%s</urlset>' % locs
  return ''


def _reset_harness():
  static.StaticContent._entities().clear()
  _memcache_store.clear()
  del _deferred_tasks[:]
  _deferred_used_names.clear()


def _clear_task_queue():
  """Drops queued/known tasks, simulating a fresh per-minute scheduling slot."""
  del _deferred_tasks[:]
  _deferred_used_names.clear()


def _pending_sitemap_regens():
  return [task for task in _deferred_tasks
          if task[0] is utils._regenerate_sitemap]


def _run_deferred_tasks():
  pending = list(_deferred_tasks)
  del _deferred_tasks[:]
  for fn, args, kwargs in pending:
    call_kwargs = {k: v for k, v in kwargs.items() if not k.startswith('_')}
    fn(*args, **call_kwargs)


class _SitemapConsistencyTest(unittest.TestCase):

  def setUp(self):
    _reset_harness()
    self._orig_render = utils.render_template
    utils.render_template = _fake_render_template
    # Avoid the outbound Google sitemap ping during regeneration.
    self._orig_ping_flag = config.google_sitemap_ping
    config.google_sitemap_ping = False

  def tearDown(self):
    utils.render_template = self._orig_render
    config.google_sitemap_ping = self._orig_ping_flag

  # -- helpers --------------------------------------------------------------

  def _indexed_paths(self):
    return sorted(utils._get_all_paths())

  def _regenerate(self):
    """Runs pending deferred work (incl. the coalesced sitemap rebuild)."""
    with mock.patch.dict(sys.modules,
                         {'gzip': _fake_gzip, 'StringIO': _fake_stringio}):
      _run_deferred_tasks()

  def _served_body(self, path):
    content = static.get(path)
    return content.body if content else None

  # -- scenario: deletion ---------------------------------------------------

  def test_deleting_indexed_content_drops_it_from_the_sitemap(self):
    static.set('/2024/01/keep', 'a', HTML)
    static.set('/2024/01/gone', 'b', HTML)
    self.assertIn('/2024/01/gone', self._indexed_paths())

    # Mirrors PostContentGenerator.generate_resource(action='delete').
    self.assertTrue(static.remove('/2024/01/gone'))

    # The deleted path leaves the sitemap's source-of-truth immediately...
    self.assertNotIn('/2024/01/gone', self._indexed_paths())
    self.assertIn('/2024/01/keep', self._indexed_paths())
    # ...and a regeneration is queued so the served files get rebuilt.
    self.assertTrue(_pending_sitemap_regens())

    self._regenerate()
    xml = self._served_body('/sitemap.xml')
    gz = self._served_body('/sitemap.xml.gz')
    self.assertIsNotNone(xml)
    self.assertIsNotNone(gz)
    for body in (xml, gz):
      self.assertIn('/2024/01/keep', body)
      self.assertNotIn('/2024/01/gone', body)

  def test_removing_indexed_resource_schedules_one_regeneration(self):
    # Isolates the fix: removal alone must schedule a rebuild.
    static.set('/post', 'body', HTML)
    self._regenerate()      # consume the set()'s rebuild and reach a clean state
    _clear_task_queue()     # simulate a later scheduling window
    self.assertEqual(_pending_sitemap_regens(), [])

    self.assertTrue(static.remove('/post'))

    regens = _pending_sitemap_regens()
    self.assertEqual(len(regens), 1)
    self.assertIs(regens[0][0], utils._regenerate_sitemap)

  # -- scenario: renaming (path change) -------------------------------------

  def test_renaming_replaces_old_path_with_new_path(self):
    static.set('/old-path', 'body', HTML)
    self.assertIn('/old-path', self._indexed_paths())

    # Mirrors handlers.PageHandler.post: publish new path, remove old path.
    static.set('/new-path', 'body', HTML)
    self.assertTrue(static.remove('/old-path'))

    paths = self._indexed_paths()
    self.assertIn('/new-path', paths)
    self.assertNotIn('/old-path', paths)

    self._regenerate()
    for body in (self._served_body('/sitemap.xml'),
                 self._served_body('/sitemap.xml.gz')):
      self.assertIn('/new-path', body)
      self.assertNotIn('/old-path', body)

  # -- scenario: regeneration ----------------------------------------------

  def test_regeneration_writes_both_sitemaps_as_non_indexed(self):
    static.set('/a', 'x', HTML)
    static.set('/b', 'y', HTML)

    self._regenerate()

    xml = static.get('/sitemap.xml')
    gz = static.get('/sitemap.xml.gz')
    self.assertIsNotNone(xml)
    self.assertIsNotNone(gz)
    self.assertIn('/a', xml.body)
    self.assertIn('/b', xml.body)

    # The sitemap files must not be indexed: they must not list themselves and
    # must not trigger a further regeneration (no infinite rebuild loop).
    self.assertFalse(xml.indexed)
    self.assertFalse(gz.indexed)
    self.assertNotIn('/sitemap.xml', xml.body)
    self.assertNotIn('/sitemap.xml.gz', xml.body)
    self.assertNotIn('/sitemap.xml', self._indexed_paths())
    self.assertNotIn('/sitemap.xml.gz', self._indexed_paths())
    self.assertEqual(_pending_sitemap_regens(), [])

  # -- scenario: non-indexed resources --------------------------------------

  def test_non_indexed_resources_never_touch_the_sitemap(self):
    # Creating a non-indexed resource neither lists it nor schedules a rebuild.
    static.set('/feeds/atom.xml', 'feed', 'application/atom+xml',
               indexed=False)
    self.assertNotIn('/feeds/atom.xml', self._indexed_paths())
    self.assertEqual(_pending_sitemap_regens(), [])

    # Removing a non-indexed resource must not schedule a rebuild either.
    self.assertFalse(static.remove('/feeds/atom.xml'))
    self.assertEqual(_pending_sitemap_regens(), [])

    # In particular, removing the (non-indexed) sitemap files is inert.
    static.set('/sitemap.xml', '<urlset/>', 'application/xml', indexed=False)
    _clear_task_queue()
    self.assertFalse(static.remove('/sitemap.xml'))
    self.assertEqual(_pending_sitemap_regens(), [])

  def test_setting_indexed_schedules_regen_unindexed_does_not(self):
    _clear_task_queue()
    static.set('/p', 'b', HTML)               # indexed defaults to True
    self.assertEqual(len(_pending_sitemap_regens()), 1)

    _clear_task_queue()
    static.set('/q', 'b', HTML, indexed=False)
    self.assertEqual(_pending_sitemap_regens(), [])

  def test_removing_missing_path_is_a_noop(self):
    self.assertFalse(static.remove('/does-not-exist'))
    self.assertEqual(_pending_sitemap_regens(), [])


if __name__ == '__main__':
  unittest.main()
