"""Regression tests for the unified post-publish pipeline.

These cover the behaviour that used to diverge between the two publish entry
points (the command-line ``script/publish`` and the admin ``PostHandler``):
force-overwrite of an already published post, static-content/memcache
consistency, draft cleanup, and the set of regenerated dependent pages.

Both entry points now funnel through ``models.BlogPost.publish`` (the CLI via
``models.publish_post``), so the tests drive each entry point and assert the
resulting site state is identical.

Running these requires Python 2 and the App Engine SDK (for
``google.appengine.ext.testbed``) plus the ``lib/aetycoon`` submodule:

    git submodule update --init
    PYTHONPATH=.:lib:$GAE_SDK python2 -m pytest tests/test_publish_pipeline.py

or, without pytest:

    PYTHONPATH=.:lib:$GAE_SDK python2 tests/test_publish_pipeline.py
"""

import base64
import datetime
import os
import sys
import unittest

# Make the application package importable when the test is run directly.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (APP_ROOT, os.path.join(APP_ROOT, 'lib')):
  if _p not in sys.path:
    sys.path.insert(0, _p)

from google.appengine.api import memcache
from google.appengine.datastore import datastore_stub_util
from google.appengine.datastore import entity_pb
from google.appengine.ext import db
from google.appengine.ext import deferred
from google.appengine.ext import testbed

import config
import models
import static
import utils


# Sitemap regeneration is a time-named deferred side effect (static.set defers
# a task named 'sitemap-<minute>'). Two publishes in the same wall-clock minute
# within one process collide on that name and tombstone it, so the path is not
# deterministic across entry points. It is not part of a post's dependent-page
# set, so it is excluded from path comparisons.
SITEMAP_PATHS = frozenset(['/sitemap.xml', '/sitemap.xml.gz'])


def _body_from_memcache(path):
  """Returns the StaticContent body cached in memcache for path, or None."""
  raw = memcache.get(path)
  if not raw:
    return None
  entity = db.model_from_protobuf(entity_pb.EntityProto(raw))
  return entity.body


class PublishPipelineTest(unittest.TestCase):

  def setUp(self):
    self.tb = testbed.Testbed()
    self.tb.activate()
    # probability=1 => writes are immediately visible to queries, so the
    # 'WHERE path =' lookup in publish_post() sees a just-published post.
    policy = datastore_stub_util.PseudoRandomHRConsistencyPolicy(probability=1)
    self.tb.init_datastore_v3_stub(consistency_policy=policy)
    self.tb.init_memcache_stub()
    self.tb.init_taskqueue_stub(root_path=APP_ROOT)
    self.taskqueue = self.tb.get_stub(testbed.TASKQUEUE_SERVICE_NAME)
    # Template rendering reads SERVER_SOFTWARE; some code reads the version id.
    os.environ['SERVER_SOFTWARE'] = 'Development/testbed'
    os.environ['CURRENT_VERSION_ID'] = 'test.1'
    # Keep the tests hermetic: no outbound sitemap / hub pings.
    config.google_sitemap_ping = False
    config.hubbub_hub_url = None

  def tearDown(self):
    self.tb.deactivate()

  # -- helpers --------------------------------------------------------------

  def run_tasks(self):
    """Runs deferred tasks (listings, archive, atom, sitemap) until drained."""
    while True:
      tasks = self.taskqueue.GetTasks('default')
      if not tasks:
        break
      self.taskqueue.FlushQueue('default')
      for task in tasks:
        deferred.run(base64.b64decode(task['body']))

  def publish_via_cli(self, title, body, tags=None, force=False):
    """The command-line path: ``script/publish`` calls ``publish_post``."""
    return models.publish_post(title, body, set(tags or []), force=force)

  def publish_via_admin(self, title, body, tags=None):
    """The admin path: ``handlers.PostHandler.post`` sets these fields and then
    calls ``BlogPost.publish()`` for a first-time publish."""
    post = models.BlogPost(title=title, body=body, tags=set(tags or []))
    post.updated = post.published = datetime.datetime.now()
    post.publish()
    return post

  def static_paths(self):
    keys = static.StaticContent.all(keys_only=True).fetch(1000)
    return set(k.name() for k in keys)

  def dependent_paths(self):
    """Generated paths excluding the non-deterministic sitemap side effect."""
    return self.static_paths() - SITEMAP_PATHS

  def reset_site(self):
    """Clears posts, generated static content and the cache between scenarios."""
    db.delete(models.BlogPost.all(keys_only=True).fetch(1000))
    db.delete(models.BlogDate.all(keys_only=True).fetch(1000))
    db.delete(static.StaticContent.all(keys_only=True).fetch(1000))
    memcache.flush_all()

  # -- tests ----------------------------------------------------------------

  def test_force_republish_unchanged_content_stays_served(self):
    """The core bug: forcing a republish of byte-identical content must not
    leave the post URL deleted or the cache stale."""
    post = self.publish_via_cli('Hello World', '<p>hi</p>', ['x'])
    self.run_tasks()
    path = post.path
    first = static.get(path)  # also primes memcache for this path
    self.assertTrue(first is not None)
    self.assertTrue('hi' in first.body)

    # Re-publish identical content with force. The old CLI deleted the static
    # entity and then skipped regeneration (etag unchanged) -> 404 / stale.
    self.publish_via_cli('Hello World', '<p>hi</p>', ['x'], force=True)
    self.run_tasks()

    # Datastore entity must still exist (the crisp invariant).
    self.assertTrue(static.StaticContent.get_by_key_name(path) is not None,
                    'post static content was deleted and not regenerated')
    served = static.get(path)
    self.assertTrue(served is not None, 'post 404s after force-republish')
    self.assertTrue('hi' in served.body)
    # Cache must agree with what is served.
    self.assertEqual(served.body, _body_from_memcache(path),
                     'memcache is stale/inconsistent after force-republish')

  def test_force_overwrite_changed_content_updates_store_and_cache(self):
    post = self.publish_via_cli('Title A', '<p>v1</p>')
    self.run_tasks()
    path = post.path
    static.get(path)  # prime cache with v1

    self.publish_via_cli('Title A', '<p>v2</p>', force=True)
    self.run_tasks()

    # Datastore (bypassing cache) reflects v2 ...
    stored = static.StaticContent.get_by_key_name(path)
    self.assertTrue('v2' in stored.body)
    self.assertTrue('v1' not in stored.body)
    # ... and so does the cache and the served view.
    served = static.get(path)
    self.assertTrue('v2' in served.body)
    self.assertEqual(served.body, _body_from_memcache(path))

  def test_publish_without_force_raises_on_existing_path(self):
    self.publish_via_cli('Dup Title', '<p>one</p>')
    self.run_tasks()
    self.assertRaises(models.PathExistsError,
                      self.publish_via_cli, 'Dup Title', '<p>two</p>')

  def test_draft_is_cleared_on_publish_from_both_entry_points(self):
    for publish in (self.publish_via_cli, self.publish_via_admin):
      title = 'Draft Me'
      draft_path = '/draft/' + utils.slugify(title)
      static.set(draft_path, '<p>draft</p>', config.html_mime_type,
                 indexed=False)
      self.assertTrue(static.get(draft_path) is not None)  # exists + primed

      publish(title, '<p>final</p>')
      self.run_tasks()

      self.assertTrue(
          static.StaticContent.get_by_key_name(draft_path) is None,
          'draft entity not removed via %s' % publish.__name__)
      self.assertTrue(memcache.get(draft_path) is None,
                      'draft memcache not cleared via %s' % publish.__name__)
      self.reset_site()

  def test_cli_and_admin_produce_the_same_site_state(self):
    cli_post = self.publish_via_cli('Same Article', '<p>same body</p>', ['t'])
    self.run_tasks()
    cli_body = static.get(cli_post.path).body
    cli_paths = self.dependent_paths()

    self.reset_site()

    admin_post = self.publish_via_admin('Same Article', '<p>same body</p>',
                                        ['t'])
    self.run_tasks()
    admin_body = static.get(admin_post.path).body
    admin_paths = self.dependent_paths()

    self.assertEqual(cli_post.path, admin_post.path)
    self.assertEqual(cli_body, admin_body)
    self.assertEqual(cli_paths, admin_paths)


if __name__ == '__main__':
  unittest.main()
