"""Regression tests for page editing, path validation, save, and delete logic.

These tests cover the bugs fixed in the page management flow:
- PageForm regex validation (anchors, allowed chars, root path)
- PageForm.clean_path duplicate detection (inverted condition, self-exclusion)
- PageHandler.post path-change flow (old_path tracking, null-check, cleanup)
- Page.hash property (references correct field)
- Page.remove safety (captures path before delete)
"""

import datetime
import hashlib
import re
import sys
import unittest
from unittest import mock


# ===========================================================================
# Python 2/3 compatibility shims
# ===========================================================================
import builtins
if not hasattr(builtins, 'basestring'):
    builtins.basestring = str

# ===========================================================================
# Minimal Django forms mock (enough to test PageForm validation)
# ===========================================================================

class _ValidationError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


class _Widget:
    def __init__(self, attrs=None):
        self.attrs = attrs or {}


class _Field:
    """Base mock field."""
    def __init__(self, widget=None, required=True, error_messages=None, **kwargs):
        self.widget = widget or _Widget()
        self.required = required
        self.error_messages = error_messages or {}
        self._kwargs = kwargs

    def clean(self, value):
        if self.required and not value:
            raise _ValidationError("This field is required.")
        return value


class _CharField(_Field):
    pass


class _BooleanField(_Field):
    def __init__(self, required=False, **kwargs):
        super().__init__(required=required, **kwargs)


class _ChoiceField(_Field):
    def __init__(self, choices=None, **kwargs):
        self.choices = choices or []
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        valid_choices = [c[0] for c in self.choices]
        if self.required and value not in valid_choices:
            raise _ValidationError("Invalid choice.")
        return value


class _RegexField(_Field):
    def __init__(self, regex=None, **kwargs):
        self.regex = re.compile(regex) if regex else None
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        if value and self.regex:
            m = self.regex.match(str(value))
            if not m:
                msg = self.error_messages.get('invalid', 'Enter a valid value.')
                raise _ValidationError(msg)
        return value


class _Textarea(_Widget):
    pass


class _TextInput(_Widget):
    pass


class _ModelFormMeta(type):
    """Metaclass that collects declared fields from the class body."""
    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)
        # Collect declared fields
        declared = {}
        for base in reversed(bases):
            if hasattr(base, '_declared_fields'):
                declared.update(base._declared_fields)
        for attr_name, attr_val in list(namespace.items()):
            if isinstance(attr_val, _Field):
                declared[attr_name] = attr_val
        cls._declared_fields = declared
        return cls


class _ModelForm(metaclass=_ModelFormMeta):
    """Minimal mock of django.forms.ModelForm."""

    class Meta:
        model = None
        fields = []

    def __init__(self, data=None, instance=None, initial=None, **kwargs):
        self.data = data or {}
        self.instance = instance
        self.initial = initial or {}
        self.errors = {}
        self.cleaned_data = {}
        self._is_valid = None
        self._kwargs = kwargs

    def _clean_fields(self):
        """Run field-level cleaning."""
        self.cleaned_data = {}
        self.errors = {}
        for name, field in self._declared_fields.items():
            value = self.data.get(name, self.initial.get(name, ''))
            try:
                self.cleaned_data[name] = field.clean(value)
            except _ValidationError as e:
                self.errors[name] = [e.message]

    def _clean_form(self):
        """Run form-level cleaning (clean_<field> methods)."""
        for name in self._declared_fields:
            clean_method = getattr(self, f'clean_{name}', None)
            if clean_method:
                try:
                    self.cleaned_data[name] = clean_method()
                except _ValidationError as e:
                    self.errors[name] = [e.message]

    def is_valid(self):
        if self._is_valid is None:
            self._clean_fields()
            self._clean_form()
            self._is_valid = len(self.errors) == 0
        return self._is_valid

    def save(self, commit=True):
        meta = getattr(self.__class__, 'Meta', None)
        model_cls = meta.model if meta else None
        if self.instance is not None:
            obj = self.instance
        else:
            obj = model_cls() if model_cls else mock.MagicMock()
        for name, value in self.cleaned_data.items():
            setattr(obj, name, value)
        if commit and hasattr(obj, 'put'):
            obj.put()
        return obj


# Build the mock forms module
_forms_mock = mock.MagicMock()
_forms_mock.CharField = _CharField
_forms_mock.BooleanField = _BooleanField
_forms_mock.ChoiceField = _ChoiceField
_forms_mock.RegexField = _RegexField
_forms_mock.Textarea = _Textarea
_forms_mock.TextInput = _TextInput
_forms_mock.ValidationError = _ValidationError
_forms_mock.ModelForm = _ModelForm

# ===========================================================================
# Mock all other GAE and application dependencies
# ===========================================================================

# db module
_db_mod = mock.MagicMock()
_db_mod.djangoforms = mock.MagicMock()
_db_mod.djangoforms.ModelForm = _ModelForm


class _MockKey:
    def __init__(self, name):
        self._name = name
    def name(self):
        return self._name
    def id(self):
        return None


_datastore = {}


class MockModel:
    """In-memory Model mock with a shared datastore."""
    _key_name = None
    _saved = False

    def __init__(self, key_name=None, **kwargs):
        if key_name is not None:
            self._key_name = key_name
        for k, v in kwargs.items():
            setattr(self, k, v)

    def key(self):
        return _MockKey(self._key_name)

    def is_saved(self):
        return self._saved

    def put(self):
        self._saved = True
        cls_name = type(self).__name__
        _datastore.setdefault(cls_name, {})[self._key_name] = self
        return self.key()

    def delete(self):
        self._saved = False
        cls_name = type(self).__name__
        _datastore.get(cls_name, {}).pop(self._key_name, None)

    @classmethod
    def get_by_key_name(cls, name):
        return _datastore.get(cls.__name__, {}).get(name)

    @classmethod
    def all(cls):
        return mock.MagicMock()


class _MockProperty:
    def __init__(self, *a, **kw):
        pass

_db_mod.Model = MockModel
_db_mod.StringProperty = _MockProperty
_db_mod.TextProperty = _MockProperty
_db_mod.DateTimeProperty = _MockProperty
_db_mod.BlobProperty = _MockProperty
_db_mod.IntegerProperty = _MockProperty
_db_mod.BooleanProperty = _MockProperty
_db_mod.StringListProperty = _MockProperty
_db_mod.run_in_transaction = lambda fn: fn()

# Install all mocked modules.
# CRITICAL: Python's "from X import Y" looks up Y as an attribute on the module
# object for X, NOT in sys.modules['X.Y']. So we must wire up both:
#   1) sys.modules['X.Y'] = child_mod  (for "import X.Y")
#   2) sys.modules['X'].Y = child_mod  (for "from X import Y")

_gae_ext = mock.MagicMock()
_gae_api = mock.MagicMock()
_gae_ds = mock.MagicMock()
_gae = mock.MagicMock()
_gae.ext = _gae_ext
_gae.api = _gae_api
_gae.datastore = _gae_ds
_google = mock.MagicMock()
_google.appengine = _gae

# Wire up the ext sub-modules
_deferred_mod = mock.MagicMock()
_deferred_mod.defer = mock.MagicMock()

class _MockRequestHandler:
    """Real base class so __new__ and inheritance work for handler tests."""
    pass

_webapp_mock = mock.MagicMock()
_webapp_mock.RequestHandler = _MockRequestHandler

_gae_ext.db = _db_mod
_gae_ext.deferred = _deferred_mod
_gae_ext.webapp = _webapp_mock
_gae_ext.webapp.util = mock.MagicMock()

sys.modules['google'] = _google
sys.modules['google.appengine'] = _gae
sys.modules['google.appengine.ext'] = _gae_ext
sys.modules['google.appengine.ext.db'] = _db_mod
sys.modules['google.appengine.ext.deferred'] = _deferred_mod
sys.modules['google.appengine.ext.webapp'] = _gae_ext.webapp
sys.modules['google.appengine.ext.webapp.util'] = mock.MagicMock()
sys.modules['google.appengine.ext.db.djangoforms'] = _db_mod.djangoforms
sys.modules['google.appengine.api'] = _gae_api
sys.modules['google.appengine.api.memcache'] = mock.MagicMock()
sys.modules['google.appengine.api.taskqueue'] = mock.MagicMock()
sys.modules['google.appengine.api.urlfetch'] = mock.MagicMock()
sys.modules['google.appengine.datastore'] = _gae_ds
sys.modules['google.appengine.datastore.entity_pb'] = mock.MagicMock()
sys.modules['google.appengine.ext.webapp.template'] = mock.MagicMock()

# Django module: "from django import forms" must resolve to _forms_mock
_django_mod = mock.MagicMock()
_django_mod.forms = _forms_mock
sys.modules['django'] = _django_mod
sys.modules['django.forms'] = _forms_mock
sys.modules['django.conf'] = mock.MagicMock()
sys.modules['django.template'] = mock.MagicMock()
sys.modules['django.template.loader'] = mock.MagicMock()
sys.modules['aetycoon'] = mock.MagicMock()

# Application module mocks
_config_mock = mock.MagicMock()
_config_mock.page_templates = {'Theme.html': 'Theme', 'Simple.html': 'Simple'}
_config_mock.html_mime_type = 'text/html; charset=utf-8'
_config_mock.default_markup = 'html'  # must be a real string for __contains__ check
_config_mock.url_prefix = ''
_config_mock.post_path_format = '/%(year)d/%(month)02d/%(slug)s'
_config_mock.blog_name = 'Test Blog'
sys.modules['config'] = _config_mock

_utils_mock = mock.MagicMock()
sys.modules['utils'] = _utils_mock

_markup_mock = mock.MagicMock()
# Use a dict-like mock that supports both iteritems() and __contains__
class _MarkupMap:
    _data = {'html': ('HTML',), 'markdown': ('Markdown',)}
    def iteritems(self):
        return iter(self._data.items())
    def __contains__(self, key):
        return key in self._data
    def __iter__(self):
        return iter(self._data)
    def items(self):
        return self._data.items()
_markup_mock.MARKUP_MAP = _MarkupMap()
sys.modules['markup'] = _markup_mock

_static_mock = mock.MagicMock()
sys.modules['static'] = _static_mock

_generators_mock = mock.MagicMock()
sys.modules['generators'] = _generators_mock

_post_deploy_mock = mock.MagicMock()
sys.modules['post_deploy'] = _post_deploy_mock

_xsrfutil_mock = mock.MagicMock()
_xsrfutil_mock.xsrf_protect = lambda fn: fn  # pass-through decorator
sys.modules['xsrfutil'] = _xsrfutil_mock


# ===========================================================================
# Import application code (uses all mocked deps)
# ===========================================================================

import handlers
import models


# ===========================================================================
# Helpers
# ===========================================================================

def _reset_datastore():
    _datastore.clear()


# ===========================================================================
# Tests: PageForm regex validation
# ===========================================================================
class TestPageFormRegex(unittest.TestCase):
    """Tests for the PageForm path regex field."""

    def _data(self, path='/about', title='Test', template='Theme.html',
              body='Hello'):
        return {'path': path, 'title': title, 'template': template, 'body': body}

    def setUp(self):
        _reset_datastore()

    def test_valid_simple_path(self):
        form = handlers.PageForm(data=self._data(path='/about'))
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_valid_root_path(self):
        form = handlers.PageForm(data=self._data(path='/'))
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_valid_nested_path(self):
        form = handlers.PageForm(data=self._data(path='/a/b/c'))
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_valid_path_with_hyphens(self):
        form = handlers.PageForm(data=self._data(path='/about-me'))
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_valid_path_with_underscores(self):
        form = handlers.PageForm(data=self._data(path='/my_page'))
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_valid_path_with_dots(self):
        form = handlers.PageForm(data=self._data(path='/file.html'))
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_valid_path_with_numbers(self):
        form = handlers.PageForm(data=self._data(path='/page123'))
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_invalid_empty_path(self):
        form = handlers.PageForm(data=self._data(path=''))
        self.assertFalse(form.is_valid())

    def test_invalid_path_no_leading_slash(self):
        form = handlers.PageForm(data=self._data(path='about'))
        self.assertFalse(form.is_valid())

    def test_invalid_path_special_chars(self):
        form = handlers.PageForm(data=self._data(path='/foo!@#'))
        self.assertFalse(form.is_valid())

    def test_invalid_path_spaces(self):
        form = handlers.PageForm(data=self._data(path='/foo bar'))
        self.assertFalse(form.is_valid())

    def test_invalid_path_backslash(self):
        form = handlers.PageForm(data=self._data(path='/foo\\bar'))
        self.assertFalse(form.is_valid())


# ===========================================================================
# Tests: PageForm.clean_path duplicate detection
# ===========================================================================
class TestPageFormCleanPath(unittest.TestCase):
    """Tests for the PageForm.clean_path duplicate-check logic."""

    def setUp(self):
        _reset_datastore()

    def _create_page(self, path, title='Existing'):
        page = models.Page(key_name=path, path=path, title=title,
                           template='Theme.html', body='body')
        page.put()
        return page

    def test_duplicate_path_rejected(self):
        """Creating a new page with an already-used path should fail."""
        self._create_page('/about')
        form = handlers.PageForm(data={
            'path': '/about', 'title': 'New', 'template': 'Theme.html',
            'body': 'content',
        }, current_path=None)
        self.assertFalse(form.is_valid())
        self.assertIn('path', form.errors)

    def test_same_path_allowed_when_editing(self):
        """Editing a page without changing its path should pass validation."""
        self._create_page('/about')
        form = handlers.PageForm(data={
            'path': '/about', 'title': 'Updated', 'template': 'Theme.html',
            'body': 'updated',
        }, current_path='/about')
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_unique_path_allowed(self):
        """A path that doesn't exist yet should pass validation."""
        form = handlers.PageForm(data={
            'path': '/new-page', 'title': 'New', 'template': 'Theme.html',
            'body': 'content',
        }, current_path=None)
        self.assertTrue(form.is_valid(), f"Errors: {form.errors}")

    def test_empty_path_rejected_by_clean_path(self):
        """An empty path should be rejected."""
        form = handlers.PageForm(data={
            'path': '', 'title': 'Empty', 'template': 'Theme.html', 'body': 'c',
        })
        self.assertFalse(form.is_valid())

    def test_different_page_taking_existing_path_rejected(self):
        """A page at /about trying to move to /contact (taken) should fail."""
        self._create_page('/contact')
        form = handlers.PageForm(data={
            'path': '/contact', 'title': 'Another', 'template': 'Theme.html',
            'body': 'content',
        }, current_path='/about')
        self.assertFalse(form.is_valid())
        self.assertIn('path', form.errors)

    def test_current_path_none_with_existing(self):
        """current_path=None with an existing page at that path should fail."""
        self._create_page('/taken')
        form = handlers.PageForm(data={
            'path': '/taken', 'title': 'T', 'template': 'Theme.html',
            'body': 'b',
        }, current_path=None)
        self.assertFalse(form.is_valid())


# ===========================================================================
# Tests: Page.hash
# ===========================================================================
class TestPageHash(unittest.TestCase):

    def setUp(self):
        _reset_datastore()

    def test_hash_does_not_raise(self):
        """Accessing page.hash should not raise AttributeError."""
        page = models.Page(key_name='/test', path='/test', title='T',
                           template='Theme.html', body='body',
                           updated=datetime.datetime.now())
        h = page.hash
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 40)

    def test_hash_changes_with_body(self):
        page = models.Page(key_name='/test', path='/test', title='T',
                           template='Theme.html', body='body1',
                           updated=datetime.datetime(2025, 1, 1))
        h1 = page.hash
        page.body = 'body2'
        self.assertNotEqual(h1, page.hash)

    def test_hash_changes_with_path(self):
        page = models.Page(key_name='/a', path='/a', title='T',
                           template='Theme.html', body='body',
                           updated=datetime.datetime(2025, 1, 1))
        h1 = page.hash
        page.path = '/b'
        self.assertNotEqual(h1, page.hash)

    def test_hash_changes_with_updated(self):
        page = models.Page(key_name='/a', path='/a', title='T',
                           template='Theme.html', body='body',
                           updated=datetime.datetime(2025, 1, 1))
        h1 = page.hash
        page.updated = datetime.datetime(2025, 6, 1)
        self.assertNotEqual(h1, page.hash)


# ===========================================================================
# Tests: Page.remove
# ===========================================================================
class TestPageRemove(unittest.TestCase):

    def setUp(self):
        _reset_datastore()
        _generators_mock.PageContentGenerator.generate_resource.reset_mock()

    def test_remove_saved_page(self):
        page = models.Page(key_name='/old', path='/old', title='Old',
                           template='Theme.html', body='body')
        page.put()
        self.assertTrue(page.is_saved())

        page.remove()
        self.assertFalse(page.is_saved())
        _generators_mock.PageContentGenerator.generate_resource.assert_called_once_with(
            page, '/old', action='delete')

    def test_remove_unsaved_page_noop(self):
        page = models.Page(key_name='/unsaved', path='/unsaved', title='T',
                           template='Theme.html', body='b')
        page.remove()
        _generators_mock.PageContentGenerator.generate_resource.assert_not_called()

    def test_remove_captures_path_before_delete(self):
        """Path must be captured before delete to avoid stale attribute issues."""
        page = models.Page(key_name='/test', path='/test', title='T',
                           template='Theme.html', body='b')
        page.put()
        page.remove()
        call_args = _generators_mock.PageContentGenerator.generate_resource.call_args
        self.assertEqual(call_args[0][1], '/test')

    def test_remove_deletes_entity_from_datastore(self):
        page = models.Page(key_name='/gone', path='/gone', title='T',
                           template='Theme.html', body='b')
        page.put()
        self.assertIsNotNone(models.Page.get_by_key_name('/gone'))

        page.remove()
        self.assertIsNone(models.Page.get_by_key_name('/gone'))


# ===========================================================================
# Tests: PageHandler.post (save and path-change flow)
# ===========================================================================
class TestPageHandlerPost(unittest.TestCase):

    def setUp(self):
        _reset_datastore()
        _generators_mock.PageContentGenerator.generate_resource.reset_mock()

    def _make_handler(self):
        handler = handlers.PageHandler.__new__(handlers.PageHandler)
        handler.request = mock.MagicMock()
        handler.response = mock.MagicMock()
        handler.response.out = mock.MagicMock()
        handler.render_to_response = mock.MagicMock()
        handler.render_form = mock.MagicMock()
        return handler

    def _post_data(self, path='/about', title='Test', template='Theme.html',
                   body='content'):
        return {'path': path, 'title': title, 'template': template, 'body': body}

    def test_create_new_page(self):
        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/new-page')

        handler.post(None)

        handler.render_to_response.assert_called_once()
        handler.render_form.assert_not_called()
        self.assertIsNotNone(models.Page.get_by_key_name('/new-page'))

    def test_edit_page_no_path_change(self):
        old_page = models.Page(key_name='/about', path='/about', title='Old',
                               template='Theme.html', body='old body')
        old_page.put()

        handler = self._make_handler()
        handler.request.POST = self._post_data(
            path='/about', title='Updated', body='new body')

        handler.post('/about')  # pass key string, decorator looks up page

        handler.render_to_response.assert_called_once()
        saved = models.Page.get_by_key_name('/about')
        self.assertIsNotNone(saved)
        self.assertEqual(saved.title, 'Updated')

    def test_edit_page_with_path_change(self):
        old_page = models.Page(key_name='/old-path', path='/old-path', title='Page',
                               template='Theme.html', body='content')
        old_page.put()

        handler = self._make_handler()
        handler.request.POST = self._post_data(
            path='/new-path', title='Page', body='content')

        handler.post('/old-path')

        handler.render_to_response.assert_called_once()
        self.assertIsNone(models.Page.get_by_key_name('/old-path'))
        self.assertIsNotNone(models.Page.get_by_key_name('/new-path'))

    def test_path_change_to_existing_path_rejected(self):
        old_page = models.Page(key_name='/old', path='/old', title='Page',
                               template='Theme.html', body='content')
        old_page.put()

        other = models.Page(key_name='/taken', path='/taken', title='Other',
                            template='Theme.html', body='other')
        other.put()

        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/taken')

        handler.post('/old')

        handler.render_form.assert_called_once()
        handler.render_to_response.assert_not_called()
        self.assertIsNotNone(models.Page.get_by_key_name('/old'))

    def test_path_change_null_check_on_old_page(self):
        """Handler must not crash if old page entity is already gone."""
        old_page = models.Page(key_name='/ghost', path='/ghost', title='Ghost',
                               template='Theme.html', body='content')
        old_page.put()

        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/new-ghost')

        # Simulate race condition: old page gone before decorator lookup
        _datastore.get('Page', {}).pop('/ghost', None)

        # with_page decorator won't find the page → 404 text output, no crash
        handler.post('/ghost')

    def test_create_page_with_root_path(self):
        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/')

        handler.post(None)  # None = new page

        handler.render_to_response.assert_called_once()
        self.assertIsNotNone(models.Page.get_by_key_name('/'))

    def test_create_page_with_nested_path(self):
        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/docs/api/v2')

        handler.post(None)

        handler.render_to_response.assert_called_once()
        self.assertIsNotNone(models.Page.get_by_key_name('/docs/api/v2'))

    def test_invalid_path_shows_form(self):
        handler = self._make_handler()
        handler.request.POST = self._post_data(path='no-leading-slash')

        handler.post(None)

        handler.render_form.assert_called_once()
        handler.render_to_response.assert_not_called()

    def test_old_path_tracking_uses_original(self):
        """old_path must come from the original page entity, not form data."""
        old_page = models.Page(key_name='/original', path='/original',
                               title='T', template='Theme.html', body='b')
        old_page.put()

        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/changed')

        handler.post('/original')

        self.assertIsNone(models.Page.get_by_key_name('/original'))
        self.assertIsNotNone(models.Page.get_by_key_name('/changed'))

    def test_path_change_removes_old_static_content(self):
        """When path changes, old static content must be cleaned up."""
        old_page = models.Page(key_name='/old', path='/old', title='T',
                               template='Theme.html', body='b')
        old_page.put()
        _generators_mock.PageContentGenerator.generate_resource.reset_mock()

        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/new')

        handler.post('/old')

        # Should have been called with 'delete' for old path
        calls = _generators_mock.PageContentGenerator.generate_resource.call_args_list
        delete_calls = [c for c in calls
                        if c[1].get('action') == 'delete']
        self.assertTrue(len(delete_calls) > 0,
                        f"Expected a delete call, got: {calls}")

    def test_create_page_updated_timestamp(self):
        """New page should get an updated timestamp set."""
        handler = self._make_handler()
        handler.request.POST = self._post_data(path='/ts-test')

        handler.post(None)

        saved = models.Page.get_by_key_name('/ts-test')
        self.assertIsNotNone(saved)
        self.assertIsNotNone(saved.updated)


# ===========================================================================
# Tests: Page.publish
# ===========================================================================
class TestPagePublish(unittest.TestCase):

    def setUp(self):
        _reset_datastore()
        _generators_mock.PageContentGenerator.generate_resource.reset_mock()

    def test_publish_creates_entity_and_static(self):
        page = models.Page(key_name=None, path='/test', title='Test',
                           template='Theme.html', body='content')
        page.publish()

        self.assertTrue(page.is_saved())
        _generators_mock.PageContentGenerator.generate_resource.assert_called_once_with(
            page, '/test')

    def test_publish_sets_key_name_to_path(self):
        page = models.Page(key_name=None, path='/my-page', title='T',
                           template='Theme.html', body='b')
        page.publish()
        self.assertEqual(page._key_name, '/my-page')


# ===========================================================================
# Tests: PageDeleteHandler
# ===========================================================================
class TestPageDeleteHandler(unittest.TestCase):

    def setUp(self):
        _reset_datastore()
        _generators_mock.PageContentGenerator.generate_resource.reset_mock()

    def _make_handler(self):
        handler = handlers.PageDeleteHandler.__new__(handlers.PageDeleteHandler)
        handler.request = mock.MagicMock()
        handler.response = mock.MagicMock()
        handler.response.out = mock.MagicMock()
        handler.render_to_response = mock.MagicMock()
        return handler

    def test_delete_existing_page(self):
        page = models.Page(key_name='/delete-me', path='/delete-me',
                           title='Del', template='Theme.html', body='b')
        page.put()

        handler = self._make_handler()
        handler.post('/delete-me')  # pass key string, decorator looks up page

        self.assertFalse(page.is_saved())
        _generators_mock.PageContentGenerator.generate_resource.assert_called_once()
        call_args = _generators_mock.PageContentGenerator.generate_resource.call_args
        self.assertEqual(call_args[0][1], '/delete-me')
        self.assertEqual(call_args[1].get('action') or (call_args[0][2] if len(call_args[0]) > 2 else None), 'delete')
        handler.render_to_response.assert_called_once()


# ===========================================================================
# Tests: Edge cases and integration scenarios
# ===========================================================================
class TestEdgeCases(unittest.TestCase):
    """Edge case and integration tests for the page editing flow."""

    def setUp(self):
        _reset_datastore()
        _generators_mock.PageContentGenerator.generate_resource.reset_mock()

    def _make_handler(self):
        handler = handlers.PageHandler.__new__(handlers.PageHandler)
        handler.request = mock.MagicMock()
        handler.response = mock.MagicMock()
        handler.response.out = mock.MagicMock()
        handler.render_to_response = mock.MagicMock()
        handler.render_form = mock.MagicMock()
        return handler

    def test_path_with_multiple_slashes(self):
        """Deeply nested path like /a/b/c/d/e should work."""
        handler = self._make_handler()
        handler.request.POST = {
            'path': '/a/b/c/d/e', 'title': 'Deep', 'template': 'Theme.html',
            'body': 'content',
        }
        handler.post(None)
        handler.render_to_response.assert_called_once()
        self.assertIsNotNone(models.Page.get_by_key_name('/a/b/c/d/e'))

    def test_create_then_rename_then_delete(self):
        """Full lifecycle: create → rename → delete should leave no orphans."""
        handler = self._make_handler()

        # Create
        handler.request.POST = {
            'path': '/lifecycle', 'title': 'L', 'template': 'Theme.html',
            'body': 'b',
        }
        handler.post(None)
        page = models.Page.get_by_key_name('/lifecycle')
        self.assertIsNotNone(page)

        # Rename
        handler2 = self._make_handler()
        handler2.request.POST = {
            'path': '/renamed', 'title': 'L', 'template': 'Theme.html',
            'body': 'b',
        }
        handler2.post('/lifecycle')
        self.assertIsNone(models.Page.get_by_key_name('/lifecycle'))
        renamed = models.Page.get_by_key_name('/renamed')
        self.assertIsNotNone(renamed)

        # Delete
        del_handler = handlers.PageDeleteHandler.__new__(handlers.PageDeleteHandler)
        del_handler.request = mock.MagicMock()
        del_handler.response = mock.MagicMock()
        del_handler.response.out = mock.MagicMock()
        del_handler.render_to_response = mock.MagicMock()
        del_handler.post('/renamed')  # pass key string
        self.assertIsNone(models.Page.get_by_key_name('/renamed'))

    def test_two_pages_different_paths_no_conflict(self):
        """Two pages at different paths should coexist without issues."""
        handler = self._make_handler()
        handler.request.POST = {
            'path': '/page-a', 'title': 'A', 'template': 'Theme.html', 'body': 'a',
        }
        handler.post(None)

        handler2 = self._make_handler()
        handler2.request.POST = {
            'path': '/page-b', 'title': 'B', 'template': 'Theme.html', 'body': 'b',
        }
        handler2.post(None)

        self.assertIsNotNone(models.Page.get_by_key_name('/page-a'))
        self.assertIsNotNone(models.Page.get_by_key_name('/page-b'))

    def test_swap_paths_between_pages(self):
        """Page A moves to B's path — should be rejected."""
        page_a = models.Page(key_name='/a', path='/a', title='A',
                             template='Theme.html', body='a')
        page_a.put()
        page_b = models.Page(key_name='/b', path='/b', title='B',
                             template='Theme.html', body='b')
        page_b.put()

        handler = self._make_handler()
        handler.request.POST = {
            'path': '/b', 'title': 'A', 'template': 'Theme.html', 'body': 'a',
        }
        handler.post('/a')

        # Should fail: /b is taken by page_b
        handler.render_form.assert_called_once()
        # page_a should still exist at /a
        self.assertIsNotNone(models.Page.get_by_key_name('/a'))


if __name__ == '__main__':
    unittest.main()
