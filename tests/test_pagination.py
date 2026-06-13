"""Regression tests for listing-page pagination generation/cleanup.

These guard the bug where the homepage / tag / archive *subsequent* pagination
pages kept serving content that no longer existed after the post set shrank:
``ListingContentGenerator.generate_resource`` walks forward writing pages but the
forward chain stops at the new last page, so pages that existed beyond it were
never deleted.

The app targets Python 2 / Google App Engine, which is not available here, so we
install lightweight fakes into ``sys.modules`` *before* importing the real
``generators`` module and then drive the genuine generator code:

  * ``deferred.defer`` runs synchronously, so the whole generate -> prune chain
    completes in-process and the fake static store ends in its final state.
  * ``static`` is an in-memory ``{path: body}`` dict.
  * ``models.BlogPost`` is a tiny in-memory datastore supporting the handful of
    query operations the generators use.

Run with::

    python3 tests/test_pagination.py
"""

import datetime
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Fake datastore (models.BlogPost / models.BlogDate)
# ---------------------------------------------------------------------------

class FakePost(object):
  """Minimal stand-in for models.BlogPost as seen by the listing generators."""

  def __init__(self, published, normalized_tags=()):
    self.published = published
    self.normalized_tags = list(normalized_tags)


class FakeQuery(object):
  """Reproduces the GAE query surface used by ListingContentGenerator.

  Supports ``order('-published')`` and ``filter(prop_op, value)`` for the
  ``published <``, ``published >=`` and ``normalized_tags =`` filters, followed
  by ``fetch(n)``. ``filter``/``order`` mutate in place (and return self), which
  is how generators.py uses them.
  """

  def __init__(self, data):
    self._data = list(data)
    self._filters = []
    self._order_field = None
    self._order_desc = False

  def order(self, spec):
    if spec.startswith('-'):
      self._order_field, self._order_desc = spec[1:], True
    else:
      self._order_field, self._order_desc = spec, False
    return self

  def filter(self, prop_op, value):
    parts = prop_op.split()
    prop = parts[0]
    op = parts[1] if len(parts) > 1 else '='
    self._filters.append((prop, op, value))
    return self

  def _matches(self, post):
    for prop, op, value in self._filters:
      actual = getattr(post, prop)
      if op == '<':
        if not actual < value:
          return False
      elif op == '<=':
        if not actual <= value:
          return False
      elif op == '>=':
        if not actual >= value:
          return False
      elif op == '>':
        if not actual > value:
          return False
      elif op == '=':
        # List-valued properties (e.g. normalized_tags) match by membership,
        # mirroring GAE's behaviour for repeated properties.
        if isinstance(actual, (list, tuple, set)):
          if value not in actual:
            return False
        elif actual != value:
          return False
      else:  # pragma: no cover - unsupported operator in tests
        raise AssertionError('unsupported operator %r' % (op,))
    return True

  def fetch(self, limit):
    rows = [p for p in self._data if self._matches(p)]
    if self._order_field is not None:
      rows.sort(key=lambda p: getattr(p, self._order_field),
                reverse=self._order_desc)
    return rows[:limit]


class BlogPost(object):
  """Fake models.BlogPost backed by a mutable class-level dataset."""

  _dataset = []

  @classmethod
  def all(cls):
    return FakeQuery(cls._dataset)


class BlogDate(object):
  """Fake models.BlogDate providing what the archive generator needs."""

  @staticmethod
  def datetime_from_key_name(key_name):
    year, month = key_name.split('/')
    # Naive datetime on purpose: every fixture timestamp in these tests is naive
    # too, so comparisons never mix aware/naive values.
    return datetime.datetime(int(year), int(month), 1)


# ---------------------------------------------------------------------------
# Install fake modules before importing the real generators module
# ---------------------------------------------------------------------------

def _register(name):
  mod = types.ModuleType(name)
  sys.modules[name] = mod
  if '.' in name:
    parent, child = name.rsplit('.', 1)
    setattr(sys.modules[parent], child, mod)
  return mod


# google.appengine.* package tree (parents first).
_register('google')
_register('google.appengine')
_register('google.appengine.api')
_register('google.appengine.ext')
_register('google.appengine.api.urlfetch')
_register('google.appengine.ext.db')
_deferred = _register('google.appengine.ext.deferred')


def _sync_defer(fn, *args, **kwargs):
  """Run deferred tasks synchronously, dropping GAE-only (_name/_eta/...) kwargs."""
  kwargs = {k: v for k, v in kwargs.items() if not k.startswith('_')}
  return fn(*args, **kwargs)


_deferred.defer = _sync_defer


# In-memory static content store.
_static = _register('static')
_static.store = {}
_static.get = lambda path: _static.store.get(path)


def _static_set(path, body, content_type, indexed=True, **kwargs):
  _static.store[path] = body
  return body


def _static_remove(path):
  _static.store.pop(path, None)


_static.set = _static_set
_static.remove = _static_remove
_static.add = lambda path, body, content_type, indexed=True, **kwargs: _static_set(
    path, body, content_type, indexed, **kwargs)


# Minimal config / utils / markup.
_config = _register('config')
_config.posts_per_page = 3
_config.html_mime_type = 'text/html; charset=utf-8'

_utils = _register('utils')
_utils.render_template = lambda *args, **kwargs: 'rendered'

_register('markup')

# Fake models used lazily inside the generators.
_models = _register('models')
_models.BlogPost = BlogPost
_models.BlogDate = BlogDate


import generators  # noqa: E402  (must follow the sys.modules setup above)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

BASE = datetime.datetime(2010, 6, 1, 12, 0, 0)


def make_posts(n, tag=None, base=BASE):
  """n posts with strictly-decreasing timestamps (index 0 = newest)."""
  return [FakePost(base - datetime.timedelta(days=i),
                   normalized_tags=[tag] if tag else [])
          for i in range(n)]


class PaginationTestBase(unittest.TestCase):

  def setUp(self):
    _static.store.clear()
    BlogPost._dataset = []

  def set_posts(self, posts):
    BlogPost._dataset = list(posts)

  # Convenience drivers (post arg is unused by listing generators).
  def gen_index(self):
    generators.IndexContentGenerator.generate_resource(None, 'index')

  def gen_tag(self, tag):
    generators.TagsContentGenerator.generate_resource(None, tag)

  def gen_archive(self, key):
    generators.ArchivePageContentGenerator.generate_resource(None, key)

  def assertPages(self, present, absent):
    for path in present:
      self.assertIn(path, _static.store,
                    '%s should exist; store=%s' % (path, sorted(_static.store)))
    for path in absent:
      self.assertNotIn(path, _static.store,
                       '%s should be gone; store=%s' % (path, sorted(_static.store)))


class IndexPaginationTest(PaginationTestBase):

  def test_shrink_removes_trailing_pages(self):
    """The core bug: pages beyond the new last page must be deleted."""
    # 7 posts, 3 per page -> /, /page/2, /page/3
    self.set_posts(make_posts(7))
    self.gen_index()
    self.assertPages(['/', '/page/2', '/page/3'], [])

    # Drop to 4 posts -> only /, /page/2. /page/3 was orphaned and must go.
    self.set_posts(make_posts(4))
    self.gen_index()
    self.assertPages(['/', '/page/2'], ['/page/3'])

  def test_exact_multiple_no_phantom_page(self):
    """6 posts (exact multiple of 3) -> exactly two pages, no /page/3."""
    self.set_posts(make_posts(6))
    self.gen_index()
    self.assertPages(['/', '/page/2'], ['/page/3'])

  def test_grow_then_shrink_converges(self):
    self.set_posts(make_posts(4))
    self.gen_index()
    self.assertPages(['/', '/page/2'], ['/page/3'])

    self.set_posts(make_posts(7))
    self.gen_index()
    self.assertPages(['/', '/page/2', '/page/3'], [])

    # Collapse to a single page: both trailing pages must be pruned.
    self.set_posts(make_posts(2))
    self.gen_index()
    self.assertPages(['/'], ['/page/2', '/page/3'])


class TagPaginationTest(PaginationTestBase):

  def test_shrink_removes_trailing_tag_page(self):
    # 5 'python' posts (+ an unrelated post that must be filtered out).
    posts = make_posts(5, tag='python')
    posts.append(FakePost(BASE - datetime.timedelta(days=99),
                          normalized_tags=['rust']))
    self.set_posts(posts)
    self.gen_tag('python')
    self.assertPages(['/tag/python', '/tag/python/2'], [])

    # Only 2 'python' posts remain -> /tag/python/2 must be removed.
    posts = make_posts(2, tag='python')
    posts.append(FakePost(BASE - datetime.timedelta(days=99),
                          normalized_tags=['rust']))
    self.set_posts(posts)
    self.gen_tag('python')
    self.assertPages(['/tag/python'], ['/tag/python/2'])


class ArchivePaginationTest(PaginationTestBase):

  def _november(self, days):
    return [FakePost(datetime.datetime(2009, 11, d, 9, 0, 0)) for d in days]

  def test_shrink_removes_trailing_archive_page(self):
    # 5 posts in 2009/11 (+ one in 2009/10 that must be filtered out).
    posts = self._november([20, 19, 18, 17, 16])
    posts.append(FakePost(datetime.datetime(2009, 10, 15, 9, 0, 0)))
    self.set_posts(posts)
    self.gen_archive('2009/11')
    self.assertPages(['/archive/2009/11/', '/archive/2009/11/2'], [])

    # Only 2 posts left in 2009/11 -> /archive/2009/11/2 must be removed.
    posts = self._november([20, 19])
    posts.append(FakePost(datetime.datetime(2009, 10, 15, 9, 0, 0)))
    self.set_posts(posts)
    self.gen_archive('2009/11')
    self.assertPages(['/archive/2009/11/'], ['/archive/2009/11/2'])


if __name__ == '__main__':
  unittest.main(verbosity=2)
