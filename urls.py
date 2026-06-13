"""URL-prefix helpers for sub-path deployments.

When bloggart is deployed under a sub-path (``config.url_prefix = '/blog'``)
the request paths the app receives are prefixed (``/blog/2009/11/post``) while
the ``StaticContent`` entities are always stored under prefix-free keys
(``/2009/11/post``).  This module owns the single, authoritative set of rules
that map an incoming request path onto its storage key, so that the front-end
(posts, listings, the Atom feed, the search page, pages), the back-end (admin)
and the special root-path resources (robots.txt, site verification) all behave
the same way regardless of whether a prefix is configured.

It is deliberately free of App Engine / Django imports so the rules can be unit
tested in isolation, without the App Engine SDK.
"""


def normalize_prefix(url_prefix):
  """Returns ``url_prefix`` in canonical form: empty, or '/seg' with no trailing
  slash.

  ``None``, ``''`` and ``'/'`` all normalize to ``''`` (no prefix), and a
  stray trailing slash (``'/blog/'``) is removed so that a small misconfiguration
  does not silently turn every page into a 404.
  """
  return (url_prefix or '').rstrip('/')


def resolve_static_path(request_path, url_prefix, root_only_files):
  """Translate an incoming request path into the ``StaticContent`` storage key.

  Args:
    request_path: The path of the incoming request (e.g. ``/blog/2009/11/x``).
    url_prefix: The configured deployment sub-path (``config.url_prefix``).
    root_only_files: An iterable of root-relative paths that must only ever be
      served from the domain root (e.g. ``['/robots.txt']``).

  Returns:
    The prefix-free storage key (a root-relative path starting with ``/``) to
    look the content up under, or ``None`` if nothing should be served for this
    request and the caller should emit a 404.

  The rules are applied identically whether or not a prefix is configured:

    * Root-only files are served only from the domain root, never from beneath
      the prefix.  ``/robots.txt`` is served; ``/blog/robots.txt`` is a 404.
    * Every other resource is served only from beneath the prefix; the prefix is
      stripped to recover the storage key.  With no prefix configured the path
      is used as-is.
  """
  prefix = normalize_prefix(url_prefix)

  # Root-only files always live at the domain root, regardless of the prefix.
  if request_path in root_only_files:
    return request_path

  if prefix:
    if request_path == prefix:
      # Bare prefix without a trailing slash maps to the index page.
      return '/'
    if request_path.startswith(prefix + '/'):
      stripped = request_path[len(prefix):]
      if stripped in root_only_files:
        # Root-only files are not served from beneath the prefix.
        return None
      return stripped
    # Outside the prefix (and not a root-only file): nothing to serve.
    return None

  # No prefix configured: serve the request path as-is.
  return request_path
