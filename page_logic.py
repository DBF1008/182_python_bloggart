"""Single source of truth for the admin "page" change-path flow.

Saving a page from the admin UI used to spread its decisions across three
places that disagreed at the edges (root path, nested path, collision with an
existing page): the form's ``clean_path``, the handler's new-vs-old entity
swap, and the static-content cleanup. That let a rename silently overwrite a
different page or leave a duplicate behind.

This module is the one place those decisions live. It is deliberately free of
any ``google.appengine`` imports so it can be unit-tested without the GAE SDK
(see ``page_logic_test.py``); the model and handler call into it.
"""

import collections
import re


# A page path: a leading slash followed by one or more '/'-separated
# alphanumeric segments, e.g. '/about' or '/docs/intro'. Anchored on both ends
# so that things like 'foo/bar' (no leading slash) or '/a/b?x' (trailing junk)
# are rejected rather than partially matched.
PATH_RE = re.compile(r'^/[a-zA-Z0-9]+(?:/[a-zA-Z0-9]+)*$')


def normalize_path(path):
  """Returns a canonical form of ``path`` (or None if ``path`` is None).

  Strips surrounding whitespace and collapses a trailing slash so that '/a'
  and '/a/' resolve to the same page (otherwise they would become two pages).
  The root '/' is preserved here; ``validate_path`` is what rejects it.
  """
  if path is None:
    return None
  path = path.strip()
  if len(path) > 1:
    path = path.rstrip('/')
  return path


def validate_path(path):
  """Normalizes and validates a page path, returning the canonical path.

  Raises ``ValueError`` with a human-readable message if the path is not a
  legal page path. This is the only definition of "what a page path may look
  like"; the form and the model both defer to it.
  """
  path = normalize_path(path)
  if not path or not path.startswith('/'):
    raise ValueError("Page path must start with '/' (for example '/about').")
  if path == '/':
    raise ValueError(
        "'/' is reserved for the blog index; choose a sub-path such as "
        "'/about'.")
  if not PATH_RE.match(path):
    raise ValueError(
        "Page path may only contain letters, digits and '/'-separated "
        "segments, for example '/about' or '/docs/intro'.")
  return path


class PageConflictError(Exception):
  """Raised when a page would overwrite a *different* page's path."""

  def __init__(self, path):
    super(PageConflictError, self).__init__(
        "A page already exists at '%s'." % path)
    self.path = path


# is_rename: True when an existing page's path is changing (which, in the
#   datastore, means a new entity at the new key_name plus removal of the old).
# old_path_to_remove: the path whose entity + static content must be cleaned up
#   after the new one is published, or None when nothing needs removing.
PageSavePlan = collections.namedtuple(
    'PageSavePlan', ['is_rename', 'old_path_to_remove'])


def resolve_page_save(original_path, new_path, exists):
  """Decides what saving a page should do.

  Args:
    original_path: the path of the page being edited, or None when creating a
      brand new page. Expected to already be normalized.
    new_path: the requested (already validated/normalized) path.
    exists: callable ``path -> bool``, True iff a stored page lives at ``path``.

  Returns:
    A ``PageSavePlan``.

  Raises:
    PageConflictError: if creating, or renaming onto, a path that a *different*
      page already occupies.
  """
  is_edit = original_path is not None
  is_rename = is_edit and new_path != original_path
  # Only a create or a rename can collide. An in-place edit keeps its own
  # path, so the page that ``exists`` at ``new_path`` is itself, not a clash.
  if (not is_edit or is_rename) and exists(new_path):
    raise PageConflictError(new_path)
  return PageSavePlan(
      is_rename=is_rename,
      old_path_to_remove=original_path if is_rename else None)
