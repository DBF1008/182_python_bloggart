"""Regression tests for page_logic, the single source of truth for the admin
page change-path flow.

These are intentionally pure: page_logic imports no google.appengine, so this
suite needs no GAE SDK and runs under any Python 2 or 3 interpreter from the
project root:

    python page_logic_test.py
    # or: python -m unittest page_logic_test

Each case maps to a way the old, split logic produced a duplicate page or
destroyed existing content.
"""

import unittest

import page_logic


class ValidatePathTest(unittest.TestCase):

  def test_simple_and_nested_ok(self):
    self.assertEqual(page_logic.validate_path('/about'), '/about')
    self.assertEqual(page_logic.validate_path('/docs/intro'), '/docs/intro')

  def test_strips_surrounding_whitespace(self):
    self.assertEqual(page_logic.validate_path('  /about  '), '/about')

  def test_normalizes_trailing_slash(self):
    # '/a' and '/a/' must collapse to one path; otherwise they become two
    # pages (a duplicate).
    self.assertEqual(page_logic.validate_path('/a/'), '/a')
    self.assertEqual(page_logic.validate_path('/a/b/'), '/a/b')

  def test_rejects_root(self):
    # '/' is the blog index path; a page there would overwrite the homepage.
    self.assertRaises(ValueError, page_logic.validate_path, '/')

  def test_rejects_missing_leading_slash(self):
    self.assertRaises(ValueError, page_logic.validate_path, 'about')
    self.assertRaises(ValueError, page_logic.validate_path, 'foo/bar')

  def test_rejects_empty_segments(self):
    self.assertRaises(ValueError, page_logic.validate_path, '//')
    self.assertRaises(ValueError, page_logic.validate_path, '/a//b')

  def test_rejects_bad_characters(self):
    self.assertRaises(ValueError, page_logic.validate_path, '/a b')
    self.assertRaises(ValueError, page_logic.validate_path, '/a?b')
    self.assertRaises(ValueError, page_logic.validate_path, '/a/b#c')

  def test_rejects_none_and_empty(self):
    self.assertRaises(ValueError, page_logic.validate_path, None)
    self.assertRaises(ValueError, page_logic.validate_path, '')


class ResolvePageSaveTest(unittest.TestCase):

  def _exists(self, *paths):
    existing = set(paths)
    return lambda p: p in existing

  def test_new_page_on_free_path(self):
    plan = page_logic.resolve_page_save(None, '/about', self._exists())
    self.assertFalse(plan.is_rename)
    self.assertIsNone(plan.old_path_to_remove)

  def test_new_page_on_occupied_path_conflicts(self):
    # Used to silently overwrite the existing page; now a hard error.
    self.assertRaises(
        page_logic.PageConflictError,
        page_logic.resolve_page_save, None, '/about', self._exists('/about'))

  def test_edit_in_place_never_self_conflicts(self):
    # Saving '/about' without changing its path must not count as a clash with
    # itself, even though '/about' already exists.
    plan = page_logic.resolve_page_save('/about', '/about',
                                        self._exists('/about'))
    self.assertFalse(plan.is_rename)
    self.assertIsNone(plan.old_path_to_remove)

  def test_rename_to_free_path_removes_old(self):
    plan = page_logic.resolve_page_save('/about', '/about-us',
                                        self._exists('/about'))
    self.assertTrue(plan.is_rename)
    self.assertEqual(plan.old_path_to_remove, '/about')

  def test_rename_onto_other_page_conflicts(self):
    # The data-loss case: renaming '/about' onto an existing '/contact' must
    # not clobber '/contact'.
    self.assertRaises(
        page_logic.PageConflictError,
        page_logic.resolve_page_save, '/about', '/contact',
        self._exists('/about', '/contact'))

  def test_trailing_slash_edit_is_not_a_rename(self):
    # Editing '/a' and submitting '/a/' normalizes to the same path, so it must
    # be an in-place edit, not a rename that would delete the page.
    new_path = page_logic.validate_path('/a/')
    plan = page_logic.resolve_page_save('/a', new_path, self._exists('/a'))
    self.assertEqual(new_path, '/a')
    self.assertFalse(plan.is_rename)
    self.assertIsNone(plan.old_path_to_remove)

  def test_conflict_error_carries_path(self):
    try:
      page_logic.resolve_page_save(None, '/about', self._exists('/about'))
    except page_logic.PageConflictError as e:
      self.assertEqual(e.path, '/about')
    else:
      self.fail('expected PageConflictError')


if __name__ == '__main__':
  unittest.main()
