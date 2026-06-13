# -*- coding: utf-8 -*-
"""Regression tests for editing an already-published BlogPost.

These cover the publish pipeline when a published post is edited in the admin:
the body URL, archive pages, tag pages and chronological prev/next links must
all be updated together, and the content left behind at the old location must be
cleaned up. The scenarios exercised here are:

  * change title          -> the body URL moves, the old page is removed and the
                             neighbours' prev/next links follow the new URL.
  * change publish time    -> the URL and archive month move, the post leaves the
                             old archive page, and an emptied month is dropped.
  * change tags            -> tag listing pages and the post's own page reflect
                             the new tag set (and drop the removed tags).
  * duplicate slug         -> colliding slugs are disambiguated with -1/-2 ...,
                             and re-saving a post never collides with itself.

This is a Google App Engine (Python 2.7) project, so the tests run on the GAE
``testbed`` with an in-memory datastore/memcache. Run them with the App Engine
SDK on the path, e.g.::

    git submodule update --init           # populate lib/aetycoon
    python2 <sdk>/dev_appserver.py --help # (just to confirm the SDK works)
    PYTHONPATH=<gae_sdk>:. python2 -m pytest tests/test_publish_pipeline.py

or with the SDK's bundled test runner. ``dev_appserver.fix_sys_path()`` below
wires up the rest of the SDK's bundled libraries (django, webob, yaml, ...).
"""

import os
import sys
import unittest

# --- App Engine / project bootstrap (mirrors appengine_config.py) ------------
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
  sys.path.insert(0, APP_ROOT)
LIB_DIR = os.path.join(APP_ROOT, 'lib')
if LIB_DIR not in sys.path:
  sys.path.insert(0, LIB_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')

try:
  # Canonical way to put the rest of the GAE SDK libraries on sys.path. If the
  # SDK root is already importable (e.g. via PYTHONPATH) this is a no-op.
  import dev_appserver
  dev_appserver.fix_sys_path()
except ImportError:
  pass

import datetime

from google.appengine.ext import deferred
from google.appengine.ext import testbed
from google.appengine.datastore import datastore_stub_util

import config
import models
import static
import utils


def _run_synchronously(obj, *args, **kwargs):
  """Drop-in replacement for ``deferred.defer`` that runs the task inline.

  The publish pipeline pushes most resource regeneration onto the task queue.
  For deterministic assertions we execute it immediately instead. Task-queue
  options (``_name``/``_eta``/``_countdown`` ...) are ignored, and the sitemap
  regeneration task is skipped to keep the tests fast and free of side effects.
  """
  for key in list(kwargs):
    if key.startswith('_'):
      del kwargs[key]
  if obj is utils._regenerate_sitemap:
    return
  return obj(*args, **kwargs)


class PublishPipelineTest(unittest.TestCase):

  def setUp(self):
    self.testbed = testbed.Testbed()
    self.testbed.activate()
    # Strong consistency: the pipeline relies on neighbour/listing/archive
    # queries seeing posts that were just written, so make every write
    # immediately visible to subsequent queries.
    policy = datastore_stub_util.PseudoRandomHRConsistencyPolicy(probability=1)
    self.testbed.init_datastore_v3_stub(consistency_policy=policy)
    self.testbed.init_memcache_stub()
    self.testbed.init_taskqueue_stub()

    # Run deferred regeneration inline and disable network side effects.
    self._orig_defer = deferred.defer
    deferred.defer = _run_synchronously
    self._orig_hub = config.hubbub_hub_url
    self._orig_ping = config.google_sitemap_ping
    config.hubbub_hub_url = None
    config.google_sitemap_ping = False

  def tearDown(self):
    deferred.defer = self._orig_defer
    config.hubbub_hub_url = self._orig_hub
    config.google_sitemap_ping = self._orig_ping
    self.testbed.deactivate()

  # --- helpers ---------------------------------------------------------------

  def _make_post(self, title, published, body=u'Body text.', tags=None):
    """Creates and publishes a post, returning the in-memory entity."""
    post = models.BlogPost(
        title=title,
        body=body,
        tags=tags if tags is not None else [],
        published=published,
        updated=published)
    post.publish()
    return post

  def _edit(self, post, **changes):
    """Applies field changes to an existing post and re-publishes it."""
    for key, value in changes.items():
      setattr(post, key, value)
    post.updated = datetime.datetime.now()
    post.publish()
    return post

  def _body_at(self, path):
    content = static.get(path)
    self.assertIsNotNone(content, 'expected static content at %s' % (path,))
    return content.body

  # --- scenario: change title ------------------------------------------------

  def test_change_title_moves_url_and_cleans_up_old(self):
    older = self._make_post(u'older post', datetime.datetime(2024, 1, 10))
    post = self._make_post(u'original title', datetime.datetime(2024, 1, 20))
    newer = self._make_post(u'newer post', datetime.datetime(2024, 1, 30))

    old_path = post.path
    self.assertEqual(old_path, '/2024/01/original-title')

    self._edit(post, title=u'updated title')

    # The body URL is recomputed from the new slug...
    self.assertEqual(post.path, '/2024/01/updated-title')
    # ...the new page exists and the stale page is gone.
    self.assertIsNotNone(static.get('/2024/01/updated-title'))
    self.assertIsNone(static.get(old_path),
                      'stale page must not be left behind at the old URL')

    # The chronological neighbours now link to the new URL, never the old one.
    older_body = self._body_at(older.path)   # older.next == post
    newer_body = self._body_at(newer.path)   # newer.prev == post
    self.assertIn('/2024/01/updated-title', older_body)
    self.assertIn('/2024/01/updated-title', newer_body)
    self.assertNotIn(old_path, older_body)
    self.assertNotIn(old_path, newer_body)

  # --- scenario: change publish time -----------------------------------------

  def test_change_publish_month_moves_url_and_archive(self):
    post = self._make_post(u'movable', datetime.datetime(2024, 1, 15))
    old_path = post.path
    self.assertEqual(old_path, '/2024/01/movable')
    self.assertIsNotNone(models.BlogDate.get_by_key_name('2024/01'))

    self._edit(post, published=datetime.datetime(2024, 2, 15))

    # URL reflects the new month; old URL removed.
    self.assertEqual(post.path, '/2024/02/movable')
    self.assertIsNotNone(static.get('/2024/02/movable'))
    self.assertIsNone(static.get(old_path))

    # New month registered; emptied old month dropped from the archive index.
    self.assertIsNotNone(models.BlogDate.get_by_key_name('2024/02'))
    self.assertIsNone(models.BlogDate.get_by_key_name('2024/01'),
                      'an emptied archive month must be cleaned up')

    # Archive pages match reality: February lists the post, January does not.
    self.assertIn('/2024/02/movable', self._body_at('/archive/2024/02/'))
    self.assertNotIn('/2024/02/movable', self._body_at('/archive/2024/01/'))

  def test_old_month_kept_when_other_posts_remain(self):
    keep = self._make_post(u'stays in jan', datetime.datetime(2024, 1, 5))
    mover = self._make_post(u'leaves jan', datetime.datetime(2024, 1, 25))

    self._edit(mover, published=datetime.datetime(2024, 3, 25))

    # January still holds 'keep', so its archive month must survive...
    self.assertIsNotNone(models.BlogDate.get_by_key_name('2024/01'))
    self.assertIsNotNone(models.BlogDate.get_by_key_name('2024/03'))
    # ...and only the moved post leaves the January archive page.
    jan_body = self._body_at('/archive/2024/01/')
    self.assertIn(keep.path, jan_body)
    self.assertNotIn(mover.path, jan_body)

  # --- scenario: change tags -------------------------------------------------

  def test_change_tags_updates_tag_pages_and_post_page(self):
    post = self._make_post(u'tagged post', datetime.datetime(2024, 1, 12),
                           tags=[u'alpha', u'beta'])
    self.assertIn(post.path, self._body_at('/tag/alpha'))
    self.assertIn(post.path, self._body_at('/tag/beta'))

    self._edit(post, tags=[u'beta', u'gamma'])

    # The removed tag no longer lists the post; kept/added tags do.
    self.assertNotIn(post.path, self._body_at('/tag/alpha'))
    self.assertIn(post.path, self._body_at('/tag/beta'))
    self.assertIn(post.path, self._body_at('/tag/gamma'))

    # The post's own page shows the new tag set and drops the removed tag.
    body = self._body_at(post.path)
    self.assertIn('/tag/beta', body)
    self.assertIn('/tag/gamma', body)
    self.assertNotIn('/tag/alpha', body)

  # --- scenario: duplicate slug ----------------------------------------------

  def test_duplicate_slug_disambiguation_and_self_collision(self):
    a = self._make_post(u'dup title', datetime.datetime(2024, 1, 2))
    b = self._make_post(u'dup title', datetime.datetime(2024, 1, 3))

    # Two posts with the same title get distinct, disambiguated URLs.
    self.assertEqual(a.path, '/2024/01/dup-title')
    self.assertEqual(b.path, '/2024/01/dup-title-1')
    self.assertIsNotNone(static.get(a.path))
    self.assertIsNotNone(static.get(b.path))

    # Re-publishing 'a' without a title change must NOT bump its suffix: a post
    # must never be treated as colliding with itself.
    self._edit(a, body=u'edited body')
    self.assertEqual(a.path, '/2024/01/dup-title')
    self.assertIsNotNone(static.get(a.path))

    # Renaming a third post onto the duplicated slug takes the next free suffix
    # and retires its previous URL.
    c = self._make_post(u'other', datetime.datetime(2024, 1, 4))
    old_c_path = c.path
    self.assertEqual(old_c_path, '/2024/01/other')

    self._edit(c, title=u'dup title')
    self.assertEqual(c.path, '/2024/01/dup-title-2')
    self.assertIsNone(static.get(old_c_path))
    self.assertIsNotNone(static.get(c.path))


if __name__ == '__main__':
  unittest.main()
