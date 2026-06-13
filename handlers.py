import datetime
import logging
import os

from google.appengine.ext import deferred
from google.appengine.ext import webapp

import config
import markup
import models
import post_deploy
import utils
import xsrfutil

from django import forms
from google.appengine.ext.db import djangoforms


class PostForm(djangoforms.ModelForm):
  title = forms.CharField(widget=forms.TextInput(attrs={'id':'name'}))
  body = forms.CharField(widget=forms.Textarea(attrs={
      'id':'message',
      'rows': 10,
      'cols': 20}))
  body_markup = forms.ChoiceField(
    choices=[(k, v[0]) for k, v in markup.MARKUP_MAP.iteritems()])
  tags = forms.CharField(widget=forms.Textarea(attrs={'rows': 5, 'cols': 20}))
  draft = forms.BooleanField(required=False)
  class Meta:
    model = models.BlogPost
    fields = [ 'title', 'body', 'tags' ]


def with_post(fun):
  def decorate(self, post_id=None):
    post = None
    if post_id:
      post = models.BlogPost.get_by_id(int(post_id))
      if not post:
        self.error(404)
        return
    fun(self, post)
  return decorate


class BaseHandler(webapp.RequestHandler):
  def render_to_response(self, template_name, template_vals=None, theme=None):
    if not template_vals:
      template_vals = {}
    template_vals.update({
        'path': self.request.path,
        'handler_class': self.__class__.__name__,
        'is_admin': True,
    })
    template_name = os.path.join("admin", template_name)
    self.response.out.write(utils.render_template(template_name, template_vals,
                                                  theme))


class AdminHandler(BaseHandler):
  def get(self):
    offset = int(self.request.get('start', 0))
    count = int(self.request.get('count', 20))
    posts = models.BlogPost.all().order('-published').fetch(count, offset)
    template_vals = {
        'offset': offset,
        'count': count,
        'last_post': offset + len(posts) - 1,
        'prev_offset': max(0, offset - count),
        'next_offset': offset + count,
        'posts': posts,
    }
    self.render_to_response("index.html", template_vals)


class PostHandler(BaseHandler):
  def render_form(self, form):
    self.render_to_response("edit.html", {'form': form})

  @with_post
  def get(self, post):
    self.render_form(PostForm(
        instance=post,
        initial={
          'draft': post and not post.path,
          'body_markup': post and post.body_markup or config.default_markup,
        }))

  @xsrfutil.xsrf_protect
  @with_post
  def post(self, post):
    form = PostForm(data=self.request.POST, instance=post,
                    initial={'draft': post and post.published is None})
    if form.is_valid():
      post = form.save(commit=False)
      if form.cleaned_data['draft']:# Draft post
        post.published = datetime.datetime.max
        post.put()
      else:
        if not post.path: # Publish post
          post.updated = post.published = datetime.datetime.now()
        else:# Edit post
          post.updated = datetime.datetime.now()
        post.publish()
      self.render_to_response("published.html", {
          'post': post,
          'draft': form.cleaned_data['draft']})
    else:
      self.render_form(form)

class DeleteHandler(BaseHandler):
  @xsrfutil.xsrf_protect
  @with_post
  def post(self, post):
    if post.path:# Published post
      post.remove()
    else:# Draft
      post.delete()
    self.render_to_response("deleted.html", None)


class PreviewHandler(BaseHandler):
  @with_post
  def get(self, post):
    # Temporary set a published date iff it's still
    # datetime.max. Django's date filter has a problem with
    # datetime.max and a "real" date looks better.
    if post.published == datetime.datetime.max:
      post.published = datetime.datetime.now()
    self.response.out.write(utils.render_template('post.html', {
        'post': post,
        'is_admin': True}))


class RegenerateHandler(BaseHandler):
  @xsrfutil.xsrf_protect
  def post(self):
    deferred.defer(post_deploy.PostRegenerator().regenerate)
    deferred.defer(post_deploy.PageRegenerator().regenerate)
    deferred.defer(post_deploy.try_post_deploy, force=True)
    self.render_to_response("regenerating.html")


class PageForm(djangoforms.ModelForm):
  path = forms.RegexField(
    widget=forms.TextInput(attrs={'id':'path'}),
    regex=r'^/([a-zA-Z0-9/_\.\-]+)?$',
    error_messages={'invalid': 'Path must start with / and contain only letters, digits, /, -, _, or .'})
  title = forms.CharField(widget=forms.TextInput(attrs={'id':'title'}))
  template = forms.ChoiceField(choices=config.page_templates.items())
  body = forms.CharField(widget=forms.Textarea(attrs={
      'id':'body',
      'rows': 10,
      'cols': 20}))
  class Meta:
    model = models.Page
    fields = [ 'path', 'title', 'template', 'body' ]

  def __init__(self, *args, **kwargs):
    self._current_path = kwargs.pop('current_path', None)
    super(PageForm, self).__init__(*args, **kwargs)

  def clean_path(self):
    data = self.cleaned_data.get('path', '')
    if not data:
      raise forms.ValidationError("Path cannot be empty.")
    existing_page = models.Page.get_by_key_name(data)
    if existing_page and data != self._current_path:
      raise forms.ValidationError("The given path already exists.")
    return data


class PageAdminHandler(BaseHandler):
  def get(self):
    offset = int(self.request.get('start', 0))
    count = int(self.request.get('count', 20))
    pages = models.Page.all().order('-updated').fetch(count, offset)
    template_vals = {
        'offset': offset,
        'count': count,
        'prev_offset': max(0, offset - count),
        'next_offset': offset + count,
        'last_page': offset + len(pages) - 1,
        'pages': pages,
    }
    self.render_to_response("indexpage.html", template_vals)


def with_page(fun):
  def decorate(self, page_key=None):
    page = None
    if page_key:
      page = models.Page.get_by_key_name(page_key)
      if not page:
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.out.write('404 :(\n' + page_key)
        #self.error(404)
        return
    fun(self, page)
  return decorate


class PageHandler(BaseHandler):
  def render_form(self, form):
    self.render_to_response("editpage.html", {'form': form})

  @with_page
  def get(self, page):
    self.render_form(PageForm(
        instance=page,
        current_path=page and page.path or None,
        initial={
          'path': page and page.path or '/',
        }))

  @xsrfutil.xsrf_protect
  @with_page
  def post(self, page):
    old_path = page.path if page else None
    new_path = self.request.POST.get('path', '')
    path_changed = page is not None and old_path != new_path

    form = PageForm(
        data=self.request.POST,
        instance=page if not path_changed else None,
        current_path=old_path,
    )
    if form.is_valid():
      page_obj = form.save(commit=False)
      page_obj.updated = datetime.datetime.now()
      page_obj.publish()
      # path edited: remove the old entity and its static content
      if path_changed and old_path:
        old_page = models.Page.get_by_key_name(old_path)
        if old_page:
          old_page.remove()
      self.render_to_response("publishedpage.html", {'page': page_obj})
    else:
      self.render_form(form)


class PageDeleteHandler(BaseHandler):
  @xsrfutil.xsrf_protect
  @with_page
  def post(self, page):
    page.remove()
    self.render_to_response("deletedpage.html", None)
