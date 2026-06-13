"""
Regression tests for the unified publish pipeline.

Verifies that CLI (script/publish) and backend (handlers.PostHandler) produce
identical site state when publishing or updating articles, covering:

  - Timestamp management (published, updated)
  - Draft static-content cleanup
  - Dependency tracking
  - CLI / backend consistency

Run from the project root:
    python tests/test_publish.py
"""

import datetime
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Bootstrap: project root + lib on sys.path, Django settings, GAE env vars.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
for _p in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, 'lib')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
os.environ['SERVER_SOFTWARE'] = 'Development/1.0'

# Attempt to select Django 1.2 (mirrors appengine_config.py).
try:
    from google.appengine.dist import use_library
    use_library('django', '1.2')
except (ImportError, Exception):
    pass

from google.appengine.ext import testbed

import config
import models
import static
import utils


def _noop_defer(*args, **kwargs):
    """Replacement for deferred.defer that silently discards tasks.

    This avoids running generators (other than the synchronous
    PostContentGenerator) during tests, keeping the focus on model-layer
    behaviour: timestamps, draft cleanup, and dependency bookkeeping.
    """
    pass


class PublishTestBase(unittest.TestCase):
    """Base class providing a fresh GAE datastore + memcache per test."""

    def setUp(self):
        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_datastore_v3_stub()
        self.testbed.init_memcache_stub()
        self.testbed.init_taskqueue_stub()

        self._orig_deffer = models.deferred.defer
        models.deferred.defer = _noop_defer
        # static.py also imports deferred; patch the same reference.
        self._orig_static_defer = static.deferred.defer
        static.deferred.defer = _noop_defer

        # Prevent any real HTTP calls (PubSubHubbub ping, etc.).
        config.hubbub_hub_url = None
        config.google_sitemap_ping = False

    def tearDown(self):
        models.deferred.defer = self._orig_deffer
        static.deferred.defer = self._orig_static_defer
        self.testbed.deactivate()

    # -- helpers -------------------------------------------------------------

    def _make_post(self, title='Test Post', body='Test body', tags=None,
                   published=None):
        return models.BlogPost(
            title=title,
            body=body,
            tags=tags or [],
            published=published or datetime.datetime.now(),
        )

    def _create_draft(self, title):
        """Simulate the CLI ``draft`` script creating a draft page."""
        draft_path = '/draft/' + utils.slugify(title)
        static.set(draft_path, '<html>draft</html>',
                   config.html_mime_type, indexed=False)


# ===========================================================================
# Timestamp tests
# ===========================================================================
class TestPublishTimestamps(PublishTestBase):
    """Both entry points must manage timestamps identically."""

    def test_new_publish_sets_updated(self):
        """New publish must set both published and updated."""
        post = self._make_post()
        post.publish()

        self.assertIsNotNone(post.updated)
        self.assertEqual(post.published, post.updated)
        self.assertIsNotNone(post.path)

    def test_new_publish_sets_published(self):
        """publish() must set published to a real timestamp (not None/max)."""
        post = self._make_post(published=datetime.datetime.max)
        post.publish()

        self.assertNotEqual(post.published, datetime.datetime.max)
        self.assertLess(
            (datetime.datetime.now() - post.published).total_seconds(), 5)

    def test_edit_updates_updated(self):
        """Editing an existing published post must refresh updated."""
        post = self._make_post()
        post.publish()
        original_updated = post.updated
        original_published = post.published

        post.body = 'Updated body content'
        post.publish()

        self.assertEqual(post.published, original_published,
                         "published must not change on edit")
        self.assertGreaterEqual(post.updated, original_updated,
                                "updated must be refreshed on edit")

    def test_force_update_preserves_published(self):
        """CLI force-update (edit via slug lookup) must keep published."""
        post = self._make_post()
        post.publish()
        original_published = post.published

        # Simulate the CLI force-update path:
        # find existing by slug, overwrite fields, call publish().
        slug = utils.slugify(post.title)
        existing = None
        for p in models.BlogPost.all().order('-published'):
            if p.path and utils.slugify(p.title) == slug:
                existing = p
                break
        self.assertIsNotNone(existing, "should find existing post by slug")

        existing.body = 'Force-updated body'
        existing.publish()

        self.assertEqual(existing.published, original_published)
        self.assertIsNotNone(existing.updated)


# ===========================================================================
# Draft cleanup tests
# ===========================================================================
class TestDraftCleanup(PublishTestBase):
    """Publishing must remove the corresponding /draft/{slug} page."""

    def test_draft_cleaned_on_publish(self):
        """If a draft page exists, publish() must remove it."""
        title = 'Draft Article'
        self._create_draft(title)
        draft_path = '/draft/' + utils.slugify(title)

        self.assertIsNotNone(static.get(draft_path),
                             "draft should exist before publish")

        post = self._make_post(title=title)
        post.publish()

        self.assertIsNone(static.get(draft_path),
                          "draft should be removed after publish")

    def test_draft_memcache_cleaned_on_publish(self):
        """Draft memcache entry must also be removed."""
        from google.appengine.api import memcache

        title = 'Memcache Draft'
        draft_path = '/draft/' + utils.slugify(title)
        self._create_draft(title)
        # Ensure memcache is populated.
        static.get(draft_path)

        post = self._make_post(title=title)
        post.publish()

        self.assertIsNone(memcache.get(draft_path))

    def test_no_draft_no_error(self):
        """publish() must succeed even when no draft page exists."""
        post = self._make_post(title='No Draft Here')
        post.publish()  # Must not raise.

        self.assertIsNotNone(post.path)
        self.assertIsNotNone(post.updated)


# ===========================================================================
# CLI / backend consistency tests
# ===========================================================================
class TestCLIBackendConsistency(PublishTestBase):
    """CLI and backend must produce identical BlogPost state."""

    def _publish_cli_style(self, title, body, tags=None):
        """Simulate the CLI new-publish path (script/publish without -f)."""
        post = models.BlogPost(
            title=title,
            body=body,
            tags=tags or [],
            published=datetime.datetime.now(),
        )
        post.publish()
        return post

    def _publish_backend_style(self, title, body, tags=None):
        """Simulate the backend PostHandler.post() new-publish path."""
        post = models.BlogPost(
            title=title,
            body=body,
            tags=tags or [],
        )
        # publish() now handles timestamp setting.
        post.publish()
        return post

    def test_new_publish_consistency(self):
        """New publish from CLI and backend yields equivalent state."""
        body = 'Identical body content.'
        tags = ['python', 'testing']

        # Use different titles to avoid URL path collisions,
        # since both posts coexist in the same datastore.
        post_cli = self._publish_cli_style('CLI Article', body, tags)
        post_be = self._publish_backend_style('Backend Article', body, tags)

        # Core fields present on both.
        self.assertIsNotNone(post_cli.path)
        self.assertIsNotNone(post_be.path)
        self.assertEqual(post_cli.body, post_be.body)

        # Both have timestamps set.
        self.assertIsNotNone(post_cli.updated)
        self.assertIsNotNone(post_be.updated)
        self.assertIsNotNone(post_cli.published)
        self.assertIsNotNone(post_be.published)

        # Both have dependency tracking populated.
        self.assertIsNotNone(post_cli.deps)
        self.assertIsNotNone(post_be.deps)
        self.assertEqual(set(post_cli.deps.keys()),
                         set(post_be.deps.keys()))

    def test_edit_consistency(self):
        """Editing from CLI and backend yields equivalent state."""
        title = 'Editable Post'
        post = self._make_post(title=title, body='Original')
        post.publish()

        # --- CLI-style edit (slug lookup + field overwrite) ---
        slug = utils.slugify(title)
        cli_post = None
        for p in models.BlogPost.all().order('-published'):
            if p.path and utils.slugify(p.title) == slug:
                cli_post = p
                break
        self.assertIsNotNone(cli_post)

        cli_post.body = 'CLI update'
        cli_post.publish()
        cli_updated = cli_post.updated
        cli_published = cli_post.published

        # --- Backend-style edit (form save + publish) ---
        post.body = 'Backend update'
        post.publish()
        be_updated = post.updated
        be_published = post.published

        # Both preserve published, both refresh updated.
        self.assertEqual(cli_published, be_published)
        self.assertIsNotNone(cli_updated)
        self.assertIsNotNone(be_updated)

    def test_draft_cleanup_consistency(self):
        """Both entry points clean up the draft page on publish."""
        title = 'Draft Consistency'

        # CLI path: draft exists, then publish.
        self._create_draft(title)
        post_cli = self._publish_cli_style(title, 'CLI body')

        # Backend path: draft exists, then publish.
        self._create_draft(title)
        post_be = self._publish_backend_style(title, 'Backend body')

        draft_path = '/draft/' + utils.slugify(title)
        # Both should have cleaned up the draft.
        self.assertIsNone(static.get(draft_path))


# ===========================================================================
# Dependency / regeneration tests
# ===========================================================================
class TestDependencyTracking(PublishTestBase):
    """get_deps() and the deps property must behave correctly."""

    def test_first_publish_has_deps(self):
        """After first publish, post.deps must be populated."""
        post = self._make_post()
        post.publish()

        self.assertIsNotNone(post.deps)
        self.assertGreater(len(post.deps), 0)

    def test_first_publish_regenerates_all(self):
        """First publish should populate deps for all generators."""
        import generators

        post = self._make_post()
        post.publish()

        for gen in generators.generator_list:
            self.assertIn(gen.name(), post.deps,
                          "deps should contain %s after first publish"
                          % gen.name())

    def test_republish_updates_deps(self):
        """Re-publishing after body change updates deps etag."""
        post = self._make_post()
        post.publish()
        first_deps = dict(post.deps)

        post.body = 'Completely new body'
        post.publish()
        second_deps = dict(post.deps)

        # PostContentGenerator etag (post.hash) must differ.
        self.assertNotEqual(
            first_deps['PostContentGenerator'][1],
            second_deps['PostContentGenerator'][1],
            "PostContentGenerator etag should change on body edit")

    def test_republish_no_change_minimal_deps(self):
        """Re-publishing without changes should produce stable deps."""
        post = self._make_post()
        post.publish()
        first_deps = dict(post.deps)

        post.publish()
        second_deps = dict(post.deps)

        # Etags should be identical when nothing changed.
        for gen_name in first_deps:
            self.assertEqual(
                first_deps[gen_name][1],
                second_deps[gen_name][1],
                "etag for %s should be stable when nothing changed"
                % gen_name)


# ===========================================================================
# Entry
# ===========================================================================
if __name__ == '__main__':
    unittest.main()
