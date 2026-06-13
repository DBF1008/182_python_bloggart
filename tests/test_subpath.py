"""Regression tests for sub-path (url_prefix) deployment support.

These tests verify that Bloggart correctly handles config.url_prefix
for sub-path deployments (e.g., deploying at /blog instead of /).

Since the project uses Python 2 syntax and depends on Google App Engine APIs,
the tests replicate each fixed logic as pure functions and also read source
files to verify the correct patterns are present (guard against reverts).
"""

import os
import re
import unittest

# ---------------------------------------------------------------------------
# Project root — one level above the tests/ directory
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ===========================================================================
# Helpers — replicate the fixed logic as testable pure functions
# ===========================================================================

def build_hubbub_url(host, url_prefix):
    """Replicate generators.py AtomContentGenerator.send_hubbub_ping URL."""
    return 'http://%s%s/feeds/atom.xml' % (host, url_prefix)


def build_google_sitemap_ping_url(host, url_prefix):
    """Replicate utils.py ping_googlesitemap URL."""
    return ('http://www.google.com/webmasters/tools/ping?sitemap=http://'
            + host + url_prefix + '/sitemap.xml.gz')


def resolve_lookup_path(request_path, url_prefix, root_only_files):
    """Replicate StaticContentHandler.get() path resolution logic.

    Returns:
        (lookup_path, should_404) tuple.
        If should_404 is True, the handler must return 404 immediately.
    """
    prefix = url_prefix
    if prefix and (request_path == prefix
                   or request_path.startswith(prefix + '/')):
        lookup_path = request_path[len(prefix):]
        if not lookup_path:
            lookup_path = '/'
        if lookup_path in root_only_files:
            return (lookup_path, True)
    else:
        lookup_path = request_path

    return (lookup_path, False)


def parse_static_fallback_path(path):
    """Replicate StaticContentHandler._try_serve_static_file path parsing.

    Returns:
        (theme_name, file_path) tuple, or None if not a valid static path.
    """
    static_prefix = '/static/'
    if not path.startswith(static_prefix):
        return None

    remainder = path[len(static_prefix):]
    parts = remainder.split('/', 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None

    theme_name, file_path = parts

    # Validate theme name
    if not all(c.isalnum() or c in '_-' for c in theme_name):
        return None

    return (theme_name, file_path)


def _read_source(relative_path):
    """Read a source file from the project root."""
    full_path = os.path.join(PROJECT_ROOT, relative_path)
    with open(full_path, 'r') as f:
        return f.read()


# ===========================================================================
# Test Classes
# ===========================================================================

ROOT_ONLY = ['/robots.txt']


class TestPubSubHubbubPingURL(unittest.TestCase):
    """PubSubHubbub hub.url must include url_prefix."""

    def test_root_deployment(self):
        url = build_hubbub_url('example.com', '')
        self.assertEqual(url, 'http://example.com/feeds/atom.xml')

    def test_subpath_deployment(self):
        url = build_hubbub_url('example.com', '/blog')
        self.assertEqual(url, 'http://example.com/blog/feeds/atom.xml')

    def test_nested_subpath(self):
        url = build_hubbub_url('example.com', '/site/blog')
        self.assertEqual(url, 'http://example.com/site/blog/feeds/atom.xml')

    def test_localhost_with_port(self):
        url = build_hubbub_url('localhost:8080', '/blog')
        self.assertEqual(url, 'http://localhost:8080/blog/feeds/atom.xml')


class TestGoogleSitemapPingURL(unittest.TestCase):
    """Google sitemap ping URL must include url_prefix."""

    def test_root_deployment(self):
        url = build_google_sitemap_ping_url('example.com', '')
        self.assertIn('sitemap=http://example.com/sitemap.xml.gz', url)

    def test_subpath_deployment(self):
        url = build_google_sitemap_ping_url('example.com', '/blog')
        self.assertIn('sitemap=http://example.com/blog/sitemap.xml.gz', url)

    def test_localhost_with_port(self):
        url = build_google_sitemap_ping_url('localhost:8080', '/blog')
        self.assertIn('sitemap=http://localhost:8080/blog/sitemap.xml.gz', url)


class TestPathResolution(unittest.TestCase):
    """StaticContentHandler.get() path resolution logic.

    Tests the core routing decision: given a request path, url_prefix,
    and ROOT_ONLY_FILES list, what internal path should be looked up?
    """

    # --- Root deployment (url_prefix = '') ---

    def test_root_homepage(self):
        path, err = resolve_lookup_path('/', '', ROOT_ONLY)
        self.assertEqual(path, '/')
        self.assertFalse(err)

    def test_root_post(self):
        path, err = resolve_lookup_path('/2024/01/hello', '', ROOT_ONLY)
        self.assertEqual(path, '/2024/01/hello')
        self.assertFalse(err)

    def test_root_robots_txt(self):
        path, err = resolve_lookup_path('/robots.txt', '', ROOT_ONLY)
        self.assertEqual(path, '/robots.txt')
        self.assertFalse(err)

    def test_root_feed(self):
        path, err = resolve_lookup_path('/feeds/atom.xml', '', ROOT_ONLY)
        self.assertEqual(path, '/feeds/atom.xml')
        self.assertFalse(err)

    def test_root_search(self):
        path, err = resolve_lookup_path('/search', '', ROOT_ONLY)
        self.assertEqual(path, '/search')
        self.assertFalse(err)

    def test_root_archive(self):
        path, err = resolve_lookup_path('/archive/', '', ROOT_ONLY)
        self.assertEqual(path, '/archive/')
        self.assertFalse(err)

    # --- Sub-path deployment (url_prefix = '/blog') ---

    def test_subpath_homepage_trailing_slash(self):
        path, err = resolve_lookup_path('/blog/', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/')
        self.assertFalse(err)

    def test_subpath_homepage_no_trailing_slash(self):
        """GET /blog (no trailing slash) must resolve to /."""
        path, err = resolve_lookup_path('/blog', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/')
        self.assertFalse(err)

    def test_subpath_post(self):
        path, err = resolve_lookup_path('/blog/2024/01/hello', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/2024/01/hello')
        self.assertFalse(err)

    def test_subpath_feed(self):
        path, err = resolve_lookup_path('/blog/feeds/atom.xml', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/feeds/atom.xml')
        self.assertFalse(err)

    def test_subpath_search(self):
        path, err = resolve_lookup_path('/blog/search', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/search')
        self.assertFalse(err)

    def test_subpath_archive(self):
        path, err = resolve_lookup_path('/blog/archive/', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/archive/')
        self.assertFalse(err)

    def test_subpath_tag_page(self):
        path, err = resolve_lookup_path('/blog/tag/python', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/tag/python')
        self.assertFalse(err)

    def test_subpath_pagination(self):
        path, err = resolve_lookup_path('/blog/page/2', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/page/2')
        self.assertFalse(err)

    def test_subpath_static_css(self):
        path, err = resolve_lookup_path(
            '/blog/static/default/css/screen.css', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/static/default/css/screen.css')
        self.assertFalse(err)

    def test_subpath_sitemap_under_prefix(self):
        path, err = resolve_lookup_path('/blog/sitemap.xml', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/sitemap.xml')
        self.assertFalse(err)

    # --- ROOT_ONLY_FILES behavior under sub-path ---

    def test_subpath_robots_at_root(self):
        """robots.txt must be servable at root even with url_prefix."""
        path, err = resolve_lookup_path('/robots.txt', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/robots.txt')
        self.assertFalse(err)

    def test_subpath_robots_under_prefix_404s(self):
        """robots.txt must NOT be served under the prefix."""
        path, err = resolve_lookup_path('/blog/robots.txt', '/blog', ROOT_ONLY)
        self.assertTrue(err)

    def test_subpath_sitemap_at_root_no_404(self):
        """sitemap.xml at root must NOT 404 — it is not a ROOT_ONLY_FILE."""
        path, err = resolve_lookup_path('/sitemap.xml', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/sitemap.xml')
        self.assertFalse(err)

    def test_subpath_feed_at_root_no_404(self):
        """Atom feed at root must NOT 404 when prefix is set."""
        path, err = resolve_lookup_path('/feeds/atom.xml', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/feeds/atom.xml')
        self.assertFalse(err)

    def test_subpath_search_at_root_no_404(self):
        """Search page at root must NOT 404 when prefix is set."""
        path, err = resolve_lookup_path('/search', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/search')
        self.assertFalse(err)

    # --- Segment boundary: partial prefix must not match ---

    def test_partial_prefix_no_match(self):
        """/blogging must NOT match prefix /blog."""
        path, err = resolve_lookup_path('/blogging', '/blog', ROOT_ONLY)
        # Should NOT strip the prefix — path stays as-is
        self.assertEqual(path, '/blogging')
        self.assertFalse(err)

    def test_partial_prefix_no_match_longer(self):
        """/blogarchive must NOT match prefix /blog."""
        path, err = resolve_lookup_path('/blogarchive', '/blog', ROOT_ONLY)
        self.assertEqual(path, '/blogarchive')
        self.assertFalse(err)

    # --- Google site verification ---

    def test_site_verification_at_root(self):
        root_only = ['/robots.txt', '/google123.html']
        path, err = resolve_lookup_path('/google123.html', '/blog', root_only)
        self.assertEqual(path, '/google123.html')
        self.assertFalse(err)

    def test_site_verification_under_prefix_404s(self):
        root_only = ['/robots.txt', '/google123.html']
        path, err = resolve_lookup_path('/blog/google123.html', '/blog', root_only)
        self.assertTrue(err)

    # --- Nested sub-path ---

    def test_nested_subpath_homepage(self):
        path, err = resolve_lookup_path('/site/blog/', '/site/blog', ROOT_ONLY)
        self.assertEqual(path, '/')
        self.assertFalse(err)

    def test_nested_subpath_post(self):
        path, err = resolve_lookup_path('/site/blog/2024/01/hello', '/site/blog', ROOT_ONLY)
        self.assertEqual(path, '/2024/01/hello')
        self.assertFalse(err)

    def test_nested_partial_prefix_no_match(self):
        """/site/blogger must NOT match prefix /site/blog."""
        path, err = resolve_lookup_path('/site/blogger', '/site/blog', ROOT_ONLY)
        self.assertEqual(path, '/site/blogger')
        self.assertFalse(err)


class TestStaticFileFallbackPathParsing(unittest.TestCase):
    """Test the static file fallback path parsing and security checks."""

    def test_valid_css_path(self):
        result = parse_static_fallback_path('/static/default/css/screen.css')
        self.assertEqual(result, ('default', 'css/screen.css'))

    def test_valid_image_path(self):
        result = parse_static_fallback_path('/static/default/images/bg.gif')
        self.assertEqual(result, ('default', 'images/bg.gif'))

    def test_valid_favicon(self):
        result = parse_static_fallback_path('/static/default/favicon.ico')
        self.assertEqual(result, ('default', 'favicon.ico'))

    def test_simple_theme(self):
        result = parse_static_fallback_path('/static/simple/css/screen.css')
        self.assertEqual(result, ('simple', 'css/screen.css'))

    def test_hyphenated_theme_name(self):
        result = parse_static_fallback_path('/static/my-theme/css/style.css')
        self.assertEqual(result, ('my-theme', 'css/style.css'))

    def test_underscored_theme_name(self):
        result = parse_static_fallback_path('/static/my_theme/js/app.js')
        self.assertEqual(result, ('my_theme', 'js/app.js'))

    def test_non_static_path(self):
        result = parse_static_fallback_path('/feeds/atom.xml')
        self.assertIsNone(result)

    def test_bare_static_no_trailing(self):
        result = parse_static_fallback_path('/static/')
        self.assertIsNone(result)

    def test_static_theme_only_no_file(self):
        result = parse_static_fallback_path('/static/default/')
        self.assertIsNone(result)

    def test_static_theme_only_no_slash(self):
        result = parse_static_fallback_path('/static/default')
        self.assertIsNone(result)

    def test_traversal_in_theme_name(self):
        result = parse_static_fallback_path('/static/../etc/passwd')
        self.assertIsNone(result)

    def test_special_chars_in_theme_name(self):
        result = parse_static_fallback_path('/static/de fault/css/screen.css')
        self.assertIsNone(result)

    def test_traversal_in_file_path_parsed_but_blocked_by_normpath(self):
        """The parser returns a result; the normpath check in the handler
        would block it. This tests the parser layer only."""
        result = parse_static_fallback_path('/static/default/../../etc/passwd')
        # Parser returns a result (theme='default', file='../../etc/passwd')
        # The actual _try_serve_static_file would block this via normpath.
        # We verify the parser extracts the parts; normpath check is separate.
        if result is not None:
            theme, file_path = result
            self.assertEqual(theme, 'default')
            self.assertIn('..', file_path)

    def test_deeply_nested_file(self):
        result = parse_static_fallback_path(
            '/static/default/css/vendor/lib/main.css')
        self.assertEqual(result, ('default', 'css/vendor/lib/main.css'))


class TestSimpleThemeTemplateVariables(unittest.TestCase):
    """Verify simple theme base.html uses url_prefix in all paths."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_source('themes/simple/base.html')

    def test_css_link_has_url_prefix(self):
        self.assertIn('{{config.url_prefix}}/static/', self.content)

    def test_atom_feed_has_url_prefix(self):
        self.assertIn('{{config.url_prefix}}/feeds/atom.xml', self.content)

    def test_search_action_has_url_prefix(self):
        self.assertIn('action="{{config.url_prefix}}/search"', self.content)

    def test_cse_xml_has_url_prefix(self):
        self.assertIn('{{config.host}}{{config.url_prefix}}/cse.xml',
                       self.content)

    def test_home_link_has_url_prefix(self):
        self.assertIn('href="{{config.url_prefix}}/"', self.content)

    def test_no_bare_static_links(self):
        """No hardcoded /static/ without url_prefix."""
        bare = re.findall(
            r'(?:href|src|action)="\s*/static/', self.content)
        self.assertEqual(bare, [],
                         "Found hardcoded /static/ links: %s" % bare)

    def test_no_bare_root_home_link(self):
        """No hardcoded href="/" without url_prefix."""
        bare = re.findall(r'href="/"', self.content)
        self.assertEqual(bare, [],
                         "Found hardcoded href=\"/\" without url_prefix")


class TestDefaultThemeTemplateVariables(unittest.TestCase):
    """Verify default theme templates use url_prefix consistently."""

    @classmethod
    def setUpClass(cls):
        cls.base = _read_source('themes/default/base.html')
        cls.listing = _read_source('themes/default/listing.html')
        cls.post = _read_source('themes/default/post.html')
        cls.archive = _read_source('themes/default/archive.html')

    def test_base_css_has_prefix(self):
        self.assertIn('{{config.url_prefix}}/static/', self.base)

    def test_base_feed_has_prefix(self):
        self.assertIn('{{config.url_prefix}}/feeds/atom.xml', self.base)

    def test_base_search_action_has_prefix(self):
        self.assertIn('action="{{config.url_prefix}}/search"', self.base)

    def test_base_home_has_prefix(self):
        self.assertIn('href="{{config.url_prefix}}/"', self.base)

    def test_base_archive_has_prefix(self):
        self.assertIn('{{config.url_prefix}}/archive/', self.base)

    def test_listing_post_links_have_prefix(self):
        self.assertIn('{{config.url_prefix}}{{post.path}}', self.listing)

    def test_listing_tag_links_have_prefix(self):
        self.assertIn('{{config.url_prefix}}/tag/', self.listing)

    def test_listing_pagination_has_prefix(self):
        self.assertIn('{{config.url_prefix}}{{prev_page}}', self.listing)
        self.assertIn('{{config.url_prefix}}{{next_page}}', self.listing)

    def test_post_tag_links_have_prefix(self):
        self.assertIn('{{config.url_prefix}}/tag/', self.post)

    def test_post_prev_next_have_prefix(self):
        self.assertIn('{{config.url_prefix}}{{prev.path}}', self.post)
        self.assertIn('{{config.url_prefix}}{{next.path}}', self.post)

    def test_archive_month_links_have_prefix(self):
        self.assertIn('{{config.url_prefix}}/archive/', self.archive)


class TestAtomFeedTemplate(unittest.TestCase):
    """Verify atom.xml template uses url_prefix for all URLs."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_source('themes/default/atom.xml')

    def test_alternate_link(self):
        self.assertIn(
            'href="http://{{config.host}}{{config.url_prefix}}/"',
            self.content)

    def test_self_link(self):
        self.assertIn(
            'href="http://{{config.host}}{{config.url_prefix}}/feeds/atom.xml"',
            self.content)

    def test_post_link(self):
        self.assertIn(
            'href="http://{{config.host}}{{config.url_prefix}}{{post.path}}"',
            self.content)

    def test_author_uri(self):
        self.assertIn(
            '<uri>http://{{config.host}}{{config.url_prefix}}/</uri>',
            self.content)


class TestSitemapTemplate(unittest.TestCase):
    """Verify sitemap.xml template uses url_prefix."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_source('themes/default/sitemap.xml')

    def test_loc_has_prefix(self):
        self.assertIn(
            '<loc>http://{{config.host}}{{config.url_prefix}}{{path}}</loc>',
            self.content)


class TestRobotsTxtTemplate(unittest.TestCase):
    """Verify robots.txt template uses url_prefix."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_source('themes/default/robots.txt')

    def test_sitemap_has_prefix(self):
        self.assertIn(
            'Sitemap: http://{{config.host}}{{config.url_prefix}}/sitemap.xml',
            self.content)


class TestCseXmlTemplate(unittest.TestCase):
    """Verify cse.xml template uses url_prefix."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_source('themes/default/cse.xml')

    def test_annotation_has_prefix(self):
        self.assertIn(
            'about="http://{{config.host}}{{config.url_prefix}}/*"',
            self.content)


class TestSourceCodePatterns(unittest.TestCase):
    """Verify the actual source files contain the correct fix patterns.

    These are guard tests: if someone reverts a fix, these tests fail.
    """

    @classmethod
    def setUpClass(cls):
        cls.generators = _read_source('generators.py')
        cls.utils = _read_source('utils.py')
        cls.static = _read_source('static.py')

    # --- generators.py ---

    def test_hubbub_url_includes_url_prefix(self):
        """PubSubHubbub hub.url must include config.url_prefix."""
        self.assertIn('config.url_prefix', self.generators)
        # Ensure the old buggy pattern is gone
        self.assertNotIn(
            "'hub.url': 'http://%s/feeds/atom.xml' % (config.host,)",
            self.generators)

    def test_hubbub_url_correct_pattern(self):
        self.assertIn(
            "'hub.url': 'http://%s%s/feeds/atom.xml' % (config.host, config.url_prefix)",
            self.generators)

    # --- utils.py ---

    def test_sitemap_ping_includes_url_prefix(self):
        """Google sitemap ping URL must include config.url_prefix."""
        # Ensure the old buggy pattern is gone
        self.assertNotIn(
            "config.host + '/sitemap.xml.gz'",
            self.utils)

    def test_sitemap_ping_correct_pattern(self):
        self.assertIn(
            "config.host + config.url_prefix + '/sitemap.xml.gz'",
            self.utils)

    # --- static.py ---

    def test_static_has_segment_safe_prefix_check(self):
        """static.py must use segment-safe prefix matching, not bare startswith."""
        # The new pattern: path == prefix or path.startswith(prefix + '/')
        self.assertIn("path.startswith(prefix + '/')", self.static)

    def test_static_has_empty_path_normalization(self):
        """static.py must normalize empty path to '/' after stripping prefix."""
        self.assertIn("lookup_path = '/'", self.static)

    def test_static_has_static_file_fallback(self):
        """static.py must have _try_serve_static_file method."""
        self.assertIn('def _try_serve_static_file', self.static)

    def test_static_has_mimetypes_import(self):
        """static.py must import mimetypes for static file serving."""
        self.assertIn('import mimetypes', self.static)

    def test_static_has_path_traversal_protection(self):
        """static.py must have normpath-based traversal protection."""
        self.assertIn('os.path.normpath', self.static)

    def test_static_no_bare_startswith_prefix(self):
        """static.py must NOT use bare path.startswith(config.url_prefix)."""
        # The old buggy pattern
        self.assertNotIn(
            'path.startswith(config.url_prefix)',
            self.static)

    def test_static_has_root_only_check_under_prefix(self):
        """static.py must block ROOT_ONLY_FILES under the prefix."""
        self.assertIn('lookup_path in ROOT_ONLY_FILES', self.static)


class TestAdminRoutes(unittest.TestCase):
    """Verify admin.py routes use config.url_prefix."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_source('admin.py')

    def test_all_routes_have_prefix(self):
        """Every route pattern must be prefixed with config.url_prefix."""
        # Find all route patterns in WSGIApplication
        routes = re.findall(r'\(config\.url_prefix\s*\+\s*\'', self.content)
        # admin.py should have 11 routes, all prefixed
        self.assertGreaterEqual(len(routes), 11)

    def test_no_unprefixed_routes(self):
        """No route should start with a bare / without config.url_prefix."""
        # Find patterns like ('/admin/ that aren't prefixed
        bare_routes = re.findall(
            r"\(\s*'/admin", self.content)
        self.assertEqual(bare_routes, [],
                         "Found unprefixed admin routes: %s" % bare_routes)


if __name__ == '__main__':
    unittest.main()
