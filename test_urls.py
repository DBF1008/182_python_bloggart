"""Regression tests for sub-path (url_prefix) deployment.

These cover the request-path -> storage-key resolution that static.py relies on
(urls.resolve_static_path) for both a root deployment (url_prefix='') and a
sub-path deployment (url_prefix='/blog'), across the scenarios that previously
behaved inconsistently:

  * normal pages (index, posts, listings, tags, archives)
  * the Atom feed
  * the search page
  * root-path reserved resources (robots.txt, site verification)

plus a guard that the 'simple' theme keeps prefixing its links.

The module under test imports no App Engine / Django code, so these run under a
plain Python interpreter without the App Engine SDK.
"""

import os
import unittest

import urls


# Mirrors static.ROOT_ONLY_FILES for the default config and for a config that
# also enables Google site verification.
ROOT_ONLY = ['/robots.txt']
ROOT_ONLY_VERIFIED = ['/robots.txt', '/google1234567890.html']

THEME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'themes')


def resolve(path, prefix, root_only=ROOT_ONLY):
  return urls.resolve_static_path(path, prefix, root_only)


class NoPrefixTest(unittest.TestCase):
  """url_prefix = '' : every in-app path resolves to itself as the storage key."""

  PREFIX = ''

  # --- normal pages -------------------------------------------------------
  def test_index(self):
    self.assertEqual(resolve('/', self.PREFIX), '/')

  def test_post(self):
    self.assertEqual(resolve('/2009/11/a-post', self.PREFIX), '/2009/11/a-post')

  def test_listing_page(self):
    self.assertEqual(resolve('/page/2', self.PREFIX), '/page/2')

  def test_tag(self):
    self.assertEqual(resolve('/tag/python', self.PREFIX), '/tag/python')

  def test_archive(self):
    self.assertEqual(resolve('/archive/2009/11/', self.PREFIX), '/archive/2009/11/')

  # --- feed ---------------------------------------------------------------
  def test_feed(self):
    self.assertEqual(resolve('/feeds/atom.xml', self.PREFIX), '/feeds/atom.xml')

  # --- search -------------------------------------------------------------
  def test_search(self):
    self.assertEqual(resolve('/search', self.PREFIX), '/search')

  # --- root-path reserved resources --------------------------------------
  def test_robots(self):
    self.assertEqual(resolve('/robots.txt', self.PREFIX), '/robots.txt')

  def test_site_verification(self):
    self.assertEqual(
        resolve('/google1234567890.html', self.PREFIX, ROOT_ONLY_VERIFIED),
        '/google1234567890.html')


class BlogPrefixTest(unittest.TestCase):
  """url_prefix = '/blog' : in-app paths are served only under the prefix and
  mapped back to their prefix-free storage key; root-only files stay at root."""

  PREFIX = '/blog'

  # --- normal pages -------------------------------------------------------
  def test_index_with_trailing_slash(self):
    self.assertEqual(resolve('/blog/', self.PREFIX), '/')

  def test_index_bare_prefix(self):
    # '/blog' (no trailing slash) must still reach the index, not 404.
    self.assertEqual(resolve('/blog', self.PREFIX), '/')

  def test_post(self):
    self.assertEqual(resolve('/blog/2009/11/a-post', self.PREFIX), '/2009/11/a-post')

  def test_listing_page(self):
    self.assertEqual(resolve('/blog/page/2', self.PREFIX), '/page/2')

  def test_tag(self):
    self.assertEqual(resolve('/blog/tag/python', self.PREFIX), '/tag/python')

  def test_archive(self):
    self.assertEqual(resolve('/blog/archive/2009/11/', self.PREFIX), '/archive/2009/11/')

  # --- feed ---------------------------------------------------------------
  def test_feed_under_prefix(self):
    self.assertEqual(resolve('/blog/feeds/atom.xml', self.PREFIX), '/feeds/atom.xml')

  def test_feed_at_root_is_404(self):
    # Under a sub-path deployment the feed lives at /blog/feeds/atom.xml; the
    # bare /feeds/atom.xml is outside the deployment.
    self.assertIsNone(resolve('/feeds/atom.xml', self.PREFIX))

  # --- search -------------------------------------------------------------
  def test_search_under_prefix(self):
    self.assertEqual(resolve('/blog/search', self.PREFIX), '/search')

  # --- root-path reserved resources --------------------------------------
  def test_robots_served_at_root(self):
    self.assertEqual(resolve('/robots.txt', self.PREFIX), '/robots.txt')

  def test_robots_under_prefix_is_404(self):
    # Root-only files are never served from beneath the prefix.
    self.assertIsNone(resolve('/blog/robots.txt', self.PREFIX))

  def test_site_verification_served_at_root(self):
    self.assertEqual(
        resolve('/google1234567890.html', self.PREFIX, ROOT_ONLY_VERIFIED),
        '/google1234567890.html')

  def test_site_verification_under_prefix_is_404(self):
    self.assertIsNone(
        resolve('/blog/google1234567890.html', self.PREFIX, ROOT_ONLY_VERIFIED))

  # --- paths outside the deployment --------------------------------------
  def test_bare_root_is_404(self):
    self.assertIsNone(resolve('/', self.PREFIX))

  def test_substring_prefix_is_not_matched(self):
    # '/blogfoo' shares a string prefix with '/blog' but is not under it.
    self.assertIsNone(resolve('/blogfoo', self.PREFIX))
    self.assertIsNone(resolve('/blog-archive/2009', self.PREFIX))


class PrefixNormalizationTest(unittest.TestCase):
  """A small misconfiguration of the prefix must not silently 404 everything."""

  def test_trailing_slash_prefix_behaves_like_no_slash(self):
    self.assertEqual(resolve('/blog/2009/11/x', '/blog/'), '/2009/11/x')
    self.assertEqual(resolve('/blog/', '/blog/'), '/')
    self.assertEqual(resolve('/blog', '/blog/'), '/')

  def test_empty_like_prefixes_serve_paths_as_is(self):
    for empty in (None, '', '/'):
      self.assertEqual(resolve('/2009/11/x', empty), '/2009/11/x')
      self.assertEqual(resolve('/robots.txt', empty), '/robots.txt')


class SimpleThemePrefixTest(unittest.TestCase):
  """Guards the 'simple' theme against re-introducing un-prefixed links."""

  def setUp(self):
    with open(os.path.join(THEME_DIR, 'simple', 'base.html')) as f:
      self.html = f.read()

  def test_no_unprefixed_links(self):
    for bad in ('href="/feeds/atom.xml"',
                'action="/search"',
                'href="/static/',
                '<a href="/">Home</a>',
                'value="http://{{config.host}}/cse.xml"'):
      self.assertNotIn(bad, self.html, 'un-prefixed link present: %s' % bad)

  def test_prefixed_links_present(self):
    for good in ('{{config.url_prefix}}/feeds/atom.xml',
                 '{{config.url_prefix}}/search',
                 '{{config.url_prefix}}/static/',
                 '{{config.host}}{{config.url_prefix}}/cse.xml',
                 '<a href="{{config.url_prefix}}/">Home</a>'):
      self.assertIn(good, self.html, 'expected prefixed link missing: %s' % good)


if __name__ == '__main__':
  unittest.main()
