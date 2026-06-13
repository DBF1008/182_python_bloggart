"""
Regression tests for listing-page pagination cleanup.

Verifies that when content shrinks (posts deleted, tags removed, archive
months emptied), stale pagination pages left over from a previous
generation pass are properly removed by ListingContentGenerator.

Run:
    python -m pytest test_pagination_cleanup.py -v
    # or
    python -m unittest test_pagination_cleanup -v
"""

import datetime
import sys
import types
import unittest

try:
    from unittest.mock import patch, MagicMock, PropertyMock
except ImportError:
    from mock import patch, MagicMock, PropertyMock  # Python 2 backport

# ---------------------------------------------------------------------------
# Mock Google App Engine runtime before importing project modules.
# The project was written for the GAE Python 2 SDK; we stub the SDK modules
# so the test can run in a standard Python environment.
# ---------------------------------------------------------------------------

_mock_modules = [
    'google',
    'google.appengine',
    'google.appengine.api',
    'google.appengine.api.memcache',
    'google.appengine.api.taskqueue',
    'google.appengine.api.urlfetch',
    'google.appengine.ext',
    'google.appengine.ext.db',
    'google.appengine.ext.deferred',
    'google.appengine.ext.webapp',
    'google.appengine.ext.webapp.template',
    'google.appengine.ext.webapp.util',
    'google.appengine.datastore',
    'google.appengine.datastore.entity_pb',
    'aetycoon',
    'django',
    'django.conf',
    'django.template',
    'django.template.loader',
    'django.utils',
    'django.utils.html',
    'django.utils.text',
    'xsrfutil',
    # Python-2-only / third-party modules used by markup.py
    'cStringIO',
    'markdown',
    'markdown_processor',
    'rst_directive',
    'textile',
    'docutils',
    'docutils.core',
]

# Python 2 built-ins that no longer exist in Python 3.
import builtins
if not hasattr(builtins, 'basestring'):
    builtins.basestring = str
if not hasattr(builtins, 'unicode'):
    builtins.unicode = str
if not hasattr(builtins, 'long'):
    builtins.long = int

_saved_modules = {}

for _name in _mock_modules:
    _saved_modules[_name] = sys.modules.get(_name)
    sys.modules[_name] = MagicMock()

# Give db a real Model base class so subclass definitions succeed.
_fake_db = sys.modules['google.appengine.ext.db']


class _FakeModel(object):
    pass


_fake_db.Model = _FakeModel

# Provide real descriptors for aetycoon so that models.BlogPost can be
# defined without a live App Engine SDK.
import hashlib as _hashlib


class _FakeTransformProperty(object):
    """Minimal descriptor mimicking aetycoon.TransformProperty.

    In the real library, @TransformProperty(source_prop) decorates a
    function that computes a derived value.  For tests we only need the
    class definition to succeed — the actual transform is never invoked.
    """

    def __init__(self, *args, **kwargs):
        self._source = args[0] if args else None
        self._func = None
        self._name = None

    def __call__(self, func):
        """When used as @TransformProperty(src) / def name(self): …"""
        self._func = func
        self._name = func.__name__
        return self

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if self._func:
            return self._func(obj, getattr(obj, self._source._name if hasattr(self._source, '_name') else '', None))
        return None

    def __set__(self, obj, value):
        pass  # derived property; ignore writes


_fake_aetycoon = sys.modules['aetycoon']
_fake_aetycoon.TransformProperty = _FakeTransformProperty
_fake_aetycoon.SetProperty = _FakeTransformProperty  # tags = SetProperty(...)
_fake_aetycoon.PickleProperty = MagicMock()
_fake_aetycoon.DerivedProperty = MagicMock()

# Make utils.tzinfo() / tz_field() work without a real timezone module.
_fake_utils_mod = sys.modules['google.appengine.ext.webapp.util']
_fake_utils_mod.tzinfo = lambda: None
_fake_utils_mod.tz_field = lambda prop: prop

# ---------------------------------------------------------------------------
# Now import project modules (they see mocked GAE APIs).
# ---------------------------------------------------------------------------

import config
import generators
from generators import (
    IndexContentGenerator,
    TagsContentGenerator,
    ArchivePageContentGenerator,
)
from post_deploy import PostRegenerator

# ===================================================================
# Test helpers
# ===================================================================


def _slugify(s):
    """Simplified slugify matching utils.slugify."""
    import re
    import unicodedata
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore')
    if isinstance(s, bytes):
        s = s.decode('ascii')
    return re.sub(r'[^a-zA-Z0-9-]+', '-', s).strip('-')


class FakeBlogPost(object):
    """Lightweight stand-in for models.BlogPost."""

    _counter = 0

    def __init__(self, title='Post', published=None, tags=None):
        FakeBlogPost._counter += 1
        self.title = title
        self.published = published
        self.tags = tags or []
        self.path = '/%d/post' % FakeBlogPost._counter
        self.deps = {}

    @property
    def normalized_tags(self):
        return list(set(_slugify(t.lower()) for t in self.tags))

    @property
    def summary_hash(self):
        return _hashlib.sha1(
            str((self.title, self.tags, self.published)).encode('utf-8')
        ).hexdigest()

    @property
    def hash(self):
        return _hashlib.sha1(
            str((self.title, self.published)).encode('utf-8')
        ).hexdigest()

    def key(self):
        return self

    def id(self):
        return FakeBlogPost._counter

    def put(self):
        pass

    def delete(self):
        pass

    def get_deps(self, regenerate=False):
        """Simplified get_deps matching models.BlogPost.get_deps."""
        import generators as _gen
        if not self.deps:
            self.deps = {}
        for generator_class in _gen.generator_list:
            new_deps = set(generator_class.get_resource_list(self))
            new_etag = generator_class.get_etag(self)
            old_deps, old_etag = self.deps.get(generator_class.name(), (set(), None))
            if new_etag != old_etag or regenerate:
                to_regenerate = new_deps | old_deps
            else:
                to_regenerate = new_deps ^ old_deps
            self.deps[generator_class.name()] = (new_deps, new_etag)
            yield generator_class, to_regenerate


class FakeQuery(object):
    """Simulates a BlogPost.all() query with chained filters and fetch."""

    def __init__(self, all_posts):
        self._posts = list(all_posts)
        self._filters = []
        self._order_desc = True

    def order(self, field):
        self._order_desc = field.startswith('-')
        return self

    def filter(self, op, value):
        self._filters.append((op, value))
        return self

    def fetch(self, limit):
        result = list(self._posts)
        for op, value in self._filters:
            parts = op.split(None, 1)
            field = parts[0]
            cmp_op = parts[1] if len(parts) > 1 else '='
            filtered = []
            for p in result:
                pv = getattr(p, field, None)
                if pv is None:
                    continue
                try:
                    if cmp_op == '<' and pv < value:
                        filtered.append(p)
                    elif cmp_op == '>' and pv > value:
                        filtered.append(p)
                    elif cmp_op == '>=' and pv >= value:
                        filtered.append(p)
                    elif cmp_op == '<=' and pv <= value:
                        filtered.append(p)
                    elif cmp_op == '=':
                        # GAE datastore treats '=' on list properties
                        # as "contains" (membership test).
                        if isinstance(pv, list):
                            if value in pv:
                                filtered.append(p)
                        elif pv == value:
                            filtered.append(p)
                    elif cmp_op == '!=':
                        if isinstance(pv, list):
                            if value not in pv:
                                filtered.append(p)
                        elif pv != value:
                            filtered.append(p)
                except TypeError:
                    continue
            result = filtered
        result.sort(key=lambda p: p.published, reverse=self._order_desc)
        return result[:limit]

    def get(self):
        """Return the first matching entity, or None."""
        results = self.fetch(1)
        return results[0] if results else None


def _make_posts(count, base_date=None, tag=None):
    """Create *count* fake posts, each one day apart."""
    base = base_date or datetime.datetime(2024, 1, 1)
    posts = []
    for i in range(count):
        p = FakeBlogPost(
            title='Post %d' % (i + 1),
            published=base + datetime.timedelta(days=i),
            tags=[tag] if tag else [],
        )
        posts.append(p)
    return posts


# ===================================================================
# Test base class
# ===================================================================


class PaginationCleanupTestCase(unittest.TestCase):
    """Base class with shared setup for pagination cleanup tests."""

    def setUp(self):
        FakeBlogPost._counter = 0
        self._static_store = {}  # path -> body
        self._deferred_queue = []  # [(fn, args, kwargs), ...]

        # -- config --
        self._orig_ppp = getattr(config, 'posts_per_page', 10)
        config.posts_per_page = 10
        config.html_mime_type = 'text/html; charset=utf-8'

        # -- static --
        self._p_static_set = patch.object(generators.static, 'set',
                                          side_effect=self._do_static_set)
        self._p_static_remove = patch.object(generators.static, 'remove',
                                             side_effect=self._do_static_remove)
        self._p_static_set.start()
        self._p_static_remove.start()

        # -- deferred (capture instead of enqueue) --
        self._p_deferred = patch.object(generators.deferred, 'defer',
                                        side_effect=self._do_defer)
        self._p_deferred.start()

        # -- utils.render_template --
        self._p_render = patch.object(generators.utils, 'render_template',
                                      return_value='<html>listing</html>')
        self._p_render.start()

        # -- models.BlogPost.all() --
        self._all_posts = []
        self._p_blogpost = patch('models.BlogPost')
        mock_bp = self._p_blogpost.start()
        mock_bp.all.side_effect = lambda: FakeQuery(self._all_posts)

        # -- models.BlogDate (used by ArchivePageContentGenerator) --
        self._p_blogdate = patch('models.BlogDate')
        mock_bd = self._p_blogdate.start()
        mock_bd.datetime_from_key_name.side_effect = self._fake_blogdate_from_key
        mock_bd.get_key_name.side_effect = lambda post: '%d/%02d' % (
            post.published.year, post.published.month)

    @staticmethod
    def _fake_blogdate_from_key(key_name):
        year, month = key_name.split('/')
        return datetime.datetime(int(year), int(month), 1)

    def tearDown(self):
        config.posts_per_page = self._orig_ppp
        self._p_static_set.stop()
        self._p_static_remove.stop()
        self._p_deferred.stop()
        self._p_render.stop()
        self._p_blogpost.stop()
        self._p_blogdate.stop()

    # -- mock side-effects --

    def _do_static_set(self, path, body, *a, **kw):
        self._static_store[path] = body

    def _do_static_remove(self, path):
        """Return truthy sentinel if page existed, None otherwise."""
        if path in self._static_store:
            del self._static_store[path]
            return '<removed:%s>' % path  # truthy
        return None

    def _do_defer(self, fn, *args, **kwargs):
        self._deferred_queue.append((fn, args, kwargs))

    # -- helpers --

    def _update_query(self):
        """Make BlogPost.all() return a fresh FakeQuery on every call.

        Using ``side_effect`` (instead of ``return_value``) ensures that
        each ``BlogPost.all()`` invocation gets a brand-new FakeQuery so
        filters applied during one page generation don't bleed into the
        next.
        """
        import models as _m
        _m.BlogPost.all.side_effect = lambda: FakeQuery(self._all_posts)

    def drain_deferred(self, max_rounds=200):
        """Execute captured deferred tasks until the queue is empty.

        Only tasks targeting generator methods we care about are executed;
        others (e.g. sitemap regeneration) are silently dropped.
        """
        targets = (
            IndexContentGenerator.generate_resource,
            TagsContentGenerator.generate_resource,
            ArchivePageContentGenerator.generate_resource,
            generators.ListingContentGenerator._remove_stale_pages,
            IndexContentGenerator._remove_stale_pages,
            TagsContentGenerator._remove_stale_pages,
            ArchivePageContentGenerator._remove_stale_pages,
        )
        rounds = 0
        while self._deferred_queue and rounds < max_rounds:
            fn, args, kwargs = self._deferred_queue.pop(0)
            if fn in targets:
                fn(*args, **kwargs)
            rounds += 1

    def page_paths(self, prefix='/page/'):
        """Return sorted list of stored pagination page paths."""
        return sorted(p for p in self._static_store if p.startswith(prefix))

    def tag_page_paths(self, tag):
        prefix = '/tag/%s' % tag
        return sorted(p for p in self._static_store if p.startswith(prefix))

    def archive_page_paths(self, resource):
        prefix = '/archive/%s/' % resource
        return sorted(p for p in self._static_store
                      if p.startswith(prefix) and p != prefix)


# ===================================================================
# Index (homepage) pagination cleanup
# ===================================================================


class TestIndexPaginationCleanup(PaginationCleanupTestCase):

    def test_single_page_no_stale_cleanup(self):
        """Posts fit on one page -> no stale pages to clean."""
        self._all_posts = _make_posts(5)
        self._update_query()

        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertEqual(self.page_paths(), [])

    def test_multi_page_generates_all(self):
        """Posts span multiple pages -> all pages generated, none stale."""
        self._all_posts = _make_posts(25)
        self._update_query()

        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertIn('/page/2', self._static_store)
        self.assertIn('/page/3', self._static_store)
        self.assertNotIn('/page/4', self._static_store)

    def test_cleanup_after_post_deletion(self):
        """Posts shrink from 3 pages to 1 -> pages 2 and 3 removed."""
        # Phase 1: 25 posts -> 3 pages
        self._all_posts = _make_posts(25)
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()
        self.assertIn('/page/2', self._static_store)
        self.assertIn('/page/3', self._static_store)

        # Phase 2: 5 posts remain
        self._all_posts = self._all_posts[:5]
        self._update_query()

        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertNotIn('/page/2', self._static_store)
        self.assertNotIn('/page/3', self._static_store)

    def test_cleanup_multiple_stale_pages(self):
        """Posts shrink from 4 pages to 1 -> pages 2-4 removed."""
        self._all_posts = _make_posts(35)
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()
        for n in range(2, 5):
            self.assertIn('/page/%d' % n, self._static_store)

        self._all_posts = self._all_posts[:5]
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        for n in range(2, 10):
            self.assertNotIn('/page/%d' % n, self._static_store)

    def test_cleanup_partial_shrinkage(self):
        """Posts shrink from 3 pages to 2 -> only page 3 removed."""
        self._all_posts = _make_posts(25)
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self._all_posts = self._all_posts[:15]
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertIn('/page/2', self._static_store)
        self.assertNotIn('/page/3', self._static_store)

    def test_no_cleanup_when_page_count_unchanged(self):
        """Same number of pages after regen -> nothing removed."""
        self._all_posts = _make_posts(15)
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()
        self.assertIn('/', self._static_store)
        self.assertIn('/page/2', self._static_store)

        # Same count, regen
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertIn('/page/2', self._static_store)

    def test_exact_boundary_no_cleanup(self):
        """Exactly posts_per_page posts -> 1 page, no stale pages."""
        self._all_posts = _make_posts(config.posts_per_page)
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertEqual(self.page_paths(), [])

    def test_one_over_boundary(self):
        """posts_per_page + 1 posts -> 2 pages, page 2 is the last."""
        self._all_posts = _make_posts(config.posts_per_page + 1)
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertIn('/page/2', self._static_store)
        self.assertNotIn('/page/3', self._static_store)


# ===================================================================
# Tag pagination cleanup
# ===================================================================


class TestTagPaginationCleanup(PaginationCleanupTestCase):

    def test_tag_cleanup_after_post_removal(self):
        """Tagged posts shrink -> stale tag pages removed."""
        tagged = _make_posts(15, tag='python')
        others = _make_posts(5, tag='other')
        self._all_posts = tagged + others
        self._update_query()

        TagsContentGenerator.generate_resource(None, 'python')
        self.drain_deferred()
        self.assertIn('/tag/python', self._static_store)
        self.assertIn('/tag/python/2', self._static_store)

        # Remove all tagged posts
        self._all_posts = others
        self._update_query()

        TagsContentGenerator.generate_resource(None, 'python')
        self.drain_deferred()

        self.assertNotIn('/tag/python/2', self._static_store)

    def test_tag_partial_cleanup(self):
        """Some tagged posts removed -> only excess pages cleaned."""
        tagged = _make_posts(25, tag='python')
        self._all_posts = tagged
        self._update_query()
        TagsContentGenerator.generate_resource(None, 'python')
        self.drain_deferred()
        self.assertIn('/tag/python/3', self._static_store)

        self._all_posts = tagged[:12]
        self._update_query()
        TagsContentGenerator.generate_resource(None, 'python')
        self.drain_deferred()

        self.assertIn('/tag/python', self._static_store)
        self.assertIn('/tag/python/2', self._static_store)
        self.assertNotIn('/tag/python/3', self._static_store)

    def test_independent_tags_not_affected(self):
        """Cleaning one tag's pages does not affect another tag."""
        alpha = _make_posts(15, tag='alpha')
        beta = _make_posts(15, tag='beta')
        self._all_posts = alpha + beta
        self._update_query()

        TagsContentGenerator.generate_resource(None, 'alpha')
        TagsContentGenerator.generate_resource(None, 'beta')
        self.drain_deferred()

        self.assertIn('/tag/alpha/2', self._static_store)
        self.assertIn('/tag/beta/2', self._static_store)

        # Remove all alpha posts
        self._all_posts = beta
        self._update_query()
        TagsContentGenerator.generate_resource(None, 'alpha')
        self.drain_deferred()

        self.assertNotIn('/tag/alpha/2', self._static_store)
        self.assertIn('/tag/beta/2', self._static_store)


# ===================================================================
# Archive pagination cleanup
# ===================================================================


class TestArchivePaginationCleanup(PaginationCleanupTestCase):

    def test_archive_cleanup(self):
        """Stale archive month pages removed after post deletion."""
        posts = []
        for i in range(25):
            p = FakeBlogPost(
                title='Archive Post %d' % (i + 1),
                published=datetime.datetime(2024, 3, 1) + datetime.timedelta(days=i),
            )
            posts.append(p)
        self._all_posts = posts
        self._update_query()

        ArchivePageContentGenerator.generate_resource(None, '2024/03')
        self.drain_deferred()
        self.assertIn('/archive/2024/03/', self._static_store)
        self.assertIn('/archive/2024/03/2', self._static_store)

        # Keep only 5
        self._all_posts = posts[:5]
        self._update_query()
        ArchivePageContentGenerator.generate_resource(None, '2024/03')
        self.drain_deferred()

        self.assertIn('/archive/2024/03/', self._static_store)
        self.assertNotIn('/archive/2024/03/2', self._static_store)

    def test_archive_empty_month(self):
        """All posts in an archive month deleted -> stale pages cleaned."""
        posts = []
        for i in range(15):
            p = FakeBlogPost(
                title='March Post %d' % (i + 1),
                published=datetime.datetime(2024, 3, 1) + datetime.timedelta(days=i),
            )
            posts.append(p)
        self._all_posts = posts
        self._update_query()

        ArchivePageContentGenerator.generate_resource(None, '2024/03')
        self.drain_deferred()
        self.assertIn('/archive/2024/03/2', self._static_store)

        # Remove all posts from this month
        self._all_posts = []
        self._update_query()
        ArchivePageContentGenerator.generate_resource(None, '2024/03')
        self.drain_deferred()

        self.assertNotIn('/archive/2024/03/2', self._static_store)


# ===================================================================
# _remove_stale_pages method — direct unit tests
# ===================================================================


class TestRemoveStalePages(PaginationCleanupTestCase):

    def test_chain_stops_at_first_gap(self):
        """Cleanup stops at the first non-existent page (no gap jumping)."""
        self._static_store['/page/2'] = 'stale'
        self._static_store['/page/3'] = 'stale'
        # gap: /page/4 does not exist
        self._static_store['/page/5'] = 'orphan beyond gap'

        IndexContentGenerator._remove_stale_pages('index', 2)
        self.drain_deferred()

        self.assertNotIn('/page/2', self._static_store)
        self.assertNotIn('/page/3', self._static_store)
        # /page/5 survives because the chain stopped at the /page/4 gap
        self.assertIn('/page/5', self._static_store)

    def test_noop_when_start_page_missing(self):
        """If the starting page doesn't exist, nothing happens."""
        IndexContentGenerator._remove_stale_pages('index', 2)
        self.drain_deferred()

        self.assertEqual(len(self._static_store), 0)

    def test_single_stale_page(self):
        """Exactly one stale page is removed."""
        self._static_store['/page/2'] = 'stale'

        IndexContentGenerator._remove_stale_pages('index', 2)
        self.drain_deferred()

        self.assertNotIn('/page/2', self._static_store)

    def test_long_chain(self):
        """Handles a long chain of consecutive stale pages."""
        for n in range(2, 12):
            self._static_store['/page/%d' % n] = 'stale'

        IndexContentGenerator._remove_stale_pages('index', 2)
        self.drain_deferred()

        for n in range(2, 12):
            self.assertNotIn('/page/%d' % n, self._static_store)

    def test_tag_path_format(self):
        """Tag cleanup uses the correct path format."""
        self._static_store['/tag/python/2'] = 'stale'
        self._static_store['/tag/python/3'] = 'stale'

        TagsContentGenerator._remove_stale_pages('python', 2)
        self.drain_deferred()

        self.assertNotIn('/tag/python/2', self._static_store)
        self.assertNotIn('/tag/python/3', self._static_store)

    def test_archive_path_format(self):
        """Archive cleanup uses the correct path format."""
        self._static_store['/archive/2024/03/2'] = 'stale'

        ArchivePageContentGenerator._remove_stale_pages('2024/03', 2)
        self.drain_deferred()

        self.assertNotIn('/archive/2024/03/2', self._static_store)


# ===================================================================
# Full rebuild flow (PostRegenerator integration)
# ===================================================================


class TestFullRebuildFlow(PaginationCleanupTestCase):

    def test_post_regenerator_cleans_stale_index_pages(self):
        """Full rebuild via PostRegenerator removes stale index pages."""
        # Phase 1: 25 posts -> 3 index pages
        self._all_posts = _make_posts(25)
        self._update_query()
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()
        self.assertIn('/page/2', self._static_store)
        self.assertIn('/page/3', self._static_store)

        # Phase 2: 5 posts remain, full rebuild
        self._all_posts = self._all_posts[:5]
        self._update_query()

        regen = PostRegenerator()
        regen.regenerate()
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertNotIn('/page/2', self._static_store)
        self.assertNotIn('/page/3', self._static_store)

    def test_rebuild_idempotent_when_nothing_changed(self):
        """Rebuilding with the same posts does not lose valid pages."""
        self._all_posts = _make_posts(25)
        self._update_query()

        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()
        self.assertIn('/page/3', self._static_store)

        # Rebuild again, same posts
        IndexContentGenerator.generate_resource(None, 'index')
        self.drain_deferred()

        self.assertIn('/', self._static_store)
        self.assertIn('/page/2', self._static_store)
        self.assertIn('/page/3', self._static_store)


if __name__ == '__main__':
    unittest.main()
