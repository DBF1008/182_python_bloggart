# -*- coding: utf-8 -*-
"""Standalone, runnable check of the post-path selection algorithm.

The full pipeline tests in ``test_publish_pipeline.py`` require Python 2 and the
App Engine SDK. This module instead re-implements *only* the path-selection
logic from ``models.BlogPost`` (``_path_is_current`` / ``_reserve_path`` and the
path decision inside ``publish``) as plain functions, so the core string logic
that disambiguates and retires URLs can be exercised directly under either
Python 2 or Python 3::

    python3 tests/test_path_logic.py

Keep these mirrors in lockstep with ``models.py`` and ``utils.format_post_path``.
Tag-page / archive regeneration is a content-generator concern and is covered by
``test_publish_pipeline.py``, not here.
"""

import re
import unicodedata
import unittest

# Mirror of config.post_path_format.
POST_PATH_FORMAT = '/%(year)d/%(month)02d/%(slug)s'


def slugify(s):
  """Mirror of utils.slugify (decode added for Python 3 byte handling)."""
  s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore')
  if isinstance(s, bytes):  # Python 3 returns bytes from .encode()
    s = s.decode('ascii')
  return re.sub('[^a-zA-Z0-9-]+', '-', s).strip('-')


def format_post_path(title, year, month, num):
  """Mirror of utils.format_post_path for the default post_path_format."""
  slug = slugify(title)
  if num > 0:
    slug += "-" + str(num)
  return POST_PATH_FORMAT % {'slug': slug, 'year': year, 'month': month}


def path_is_current(current_path, title, year, month):
  """Mirror of models.BlogPost._path_is_current."""
  if not current_path:
    return False
  base = format_post_path(title, year, month, 0)
  if current_path == base:
    return True
  prefix = base + '-'
  suffix = current_path[len(prefix):]
  return current_path.startswith(prefix) and suffix.isdigit()


def reserve_path(title, year, month, taken):
  """Mirror of models.BlogPost._reserve_path.

  ``taken`` models the set of paths that already have static content; reserving
  a path adds it to the set (as static.add does atomically).
  """
  num = 0
  while True:
    path = format_post_path(title, year, month, num)
    if path not in taken:
      taken.add(path)
      return path
    num += 1


def simulate_publish(current_path, title, year, month, taken):
  """Mirror of the path decision inside models.BlogPost.publish.

  Returns the post's path after publishing, mutating ``taken`` to reserve the
  new path and (when the path moves) release the old one (static.remove).
  """
  if path_is_current(current_path, title, year, month):
    return current_path
  old_path = current_path
  new_path = reserve_path(title, year, month, taken)
  if old_path and old_path != new_path:
    taken.discard(old_path)
  return new_path


class PathSelectionAlgorithmTest(unittest.TestCase):

  def test_change_title_moves_path_and_frees_old(self):
    taken = set()
    a = simulate_publish(None, u'original title', 2024, 1, taken)
    self.assertEqual(a, '/2024/01/original-title')

    a2 = simulate_publish(a, u'updated title', 2024, 1, taken)
    self.assertEqual(a2, '/2024/01/updated-title')
    self.assertNotIn(a, taken)          # old URL released
    self.assertIn(a2, taken)            # new URL reserved

  def test_change_publish_month_moves_path(self):
    taken = set()
    p = simulate_publish(None, u'movable', 2024, 1, taken)
    self.assertEqual(p, '/2024/01/movable')

    p2 = simulate_publish(p, u'movable', 2024, 2, taken)
    self.assertEqual(p2, '/2024/02/movable')
    self.assertNotIn(p, taken)
    self.assertIn(p2, taken)

  def test_tags_only_change_keeps_path(self):
    # Tags do not feed into the path; re-publishing keeps the same URL and does
    # not reserve a new one.
    taken = set()
    p = simulate_publish(None, u'tagged post', 2024, 1, taken)
    snapshot = set(taken)
    p2 = simulate_publish(p, u'tagged post', 2024, 1, taken)
    self.assertEqual(p2, p)
    self.assertEqual(taken, snapshot)

  def test_duplicate_slug_disambiguation_and_self_collision(self):
    taken = set()
    a = simulate_publish(None, u'dup title', 2024, 1, taken)
    b = simulate_publish(None, u'dup title', 2024, 1, taken)
    self.assertEqual(a, '/2024/01/dup-title')
    self.assertEqual(b, '/2024/01/dup-title-1')

    # Re-publishing either post without a title change must keep its own path,
    # never collide with itself and bump the suffix.
    self.assertEqual(simulate_publish(a, u'dup title', 2024, 1, taken), a)
    self.assertEqual(simulate_publish(b, u'dup title', 2024, 1, taken), b)
    self.assertEqual(taken, {a, b})

    # A third post renamed onto the duplicated slug takes the next free suffix
    # and releases its old path.
    c = simulate_publish(None, u'other', 2024, 1, taken)
    self.assertEqual(c, '/2024/01/other')
    c2 = simulate_publish(c, u'dup title', 2024, 1, taken)
    self.assertEqual(c2, '/2024/01/dup-title-2')
    self.assertNotIn(c, taken)
    self.assertEqual(taken, {a, b, c2})


if __name__ == '__main__':
  unittest.main()
