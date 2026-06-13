import datetime
import logging
import os

from google.appengine.ext import deferred
from google.appengine.ext import webapp

import config
import markup
import models
import page_logic
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
  path = forms.CharField(widget=forms.TextInput(attrs={'id':'path'}))
  title = forms.CharField(widget=forms.TextInput(attrs={'id':'title'}))
  template = forms.ChoiceField(choices=config.page_templates.items())
  body = forms.CharField(widget=forms.Textarea(attrs={
      'id':'body',
      'rows': 10,
      'cols': 20}))
  class Meta:
    model = models.Page
    fields = [ 'path', 'title', 'template', 'body' ]

  def clean_path(self):
    # Path format rules live in page_logic so the form, the model and the
    # tests all agree on what a legal page path is.
    try:
      path = page_logic.validate_path(self._cleaned_data()['path'])
    except ValueError, e:
      raise forms.ValidationError(str(e))
    # A page may keep its own path; only a *different* page already occupying
    # this path is a collision.
    original = None
    if self.instance and self.instance.is_saved():
      original = self.instance.path
    if path != original and models.Page.get_by_key_name(path) is not None:
      raise forms.ValidationError("A page already exists at '%s'." % path)
    return path


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
        initial={
          'path': page and page.path or '/',
        }))

  @xsrfutil.xsrf_protect
  @with_page
  def post(self, page):
    # Capture the original path before validating: djangoforms copies the
    # submitted fields onto `page` during is_valid(), which would otherwise
    # make a rename look like an in-place edit.
    original_path = page.path if page else None
    # Always bind to the page being edited (or None for a new page); the
    # create / edit / rename decision is made inside models.Page.save_page.
    form = PageForm(data=self.request.POST, instance=page)
    if form.is_valid():
      data = form.cleaned_data
      saved = models.Page.save_page(
          original_path,
          data['path'], data['title'], data['template'], data['body'])
      self.render_to_response("publishedpage.html", {'page': saved})
    else:
      self.render_form(form)


class PageDeleteHandler(BaseHandler):
  @xsrfutil.xsrf_protect
  @with_page
  def post(self, page):
    page.remove()
    self.render_to_response("deletedpage.html", None)
