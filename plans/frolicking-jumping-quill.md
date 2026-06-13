# 页面编辑链路重构计划

## Context

后台编辑独立页面（Page）时，"改路径"这条旧链路在以下三方面各有一套独立判断，导致边界条件下行为不一致：

1. **表单校验** (`PageForm.clean_path`) — 条件逻辑反转，实际从未拦截重复路径
2. **新旧实体切换** (`PageHandler.post`) — 路径变更时先建新实体再删旧实体，无 null-check，且依赖私有 API `_cleaned_data()`
3. **静态页面清理** (`Page.remove` / `Page.publish`) — 改路径时旧 StaticContent 的清理与新内容发布的顺序依赖脆弱

遇到根路径 `/`、嵌套路径 `/a/b/c`、或与已有页面路径冲突时，很容易留下重复页面实体或误删旧内容。

---

## 发现的 Bug 清单

### Bug 1: `PageForm.clean_path` 条件反转 (handlers.py:161)
```python
if not data and existing_page:  # 永远不会为 True
```
- `not data` 意味着 data 为空字符串/None，此时 `get_by_key_name('')` 几乎不会返回已有页面
- 应该是 `if data and existing_page`
- 但编辑同一页面时路径没变也会误报 —— 需要排除"当前正在编辑的页面自身"
- 此校验从未实际生效，**所有重复路径都能保存**

### Bug 2: `PageForm` 正则过于严格且缺少锚定 (handlers.py:147)
```python
regex='(/[a-zA-Z0-9/]+)'
```
- 无 `$` 锚定 → `/foo!@#` 能通过校验
- 不允许 `-`、`_`、`.` 等常见路径字符
- 不允许根路径 `/`（`+` 要求至少一个 `[a-zA-Z0-9/]`，但单独 `/` 不匹配）
- 而表单初始值恰好是 `'/'`，用户不改就提交会被正则拒绝

### Bug 3: `PageHandler.post` 改路径流程脆弱 (handlers.py:210-230)
- 路径变更时 `instance=None` 创建新表单，但 `form._cleaned_data()` 使用私有 API
- `oldpath` 变量赋值逻辑混乱：先取新路径，再用 `if page` 覆盖
- `oldpage.remove()` 无 null-check，若旧实体已被并发删除则 crash
- 新路径若与已有页面冲突（因 Bug 1 校验未生效），`publish()` 会直接覆盖他人实体

### Bug 4: `Page.hash` 引用不存在的属性 (models.py:166)
```python
val = (self.path, self.body, self.published)  # Page 没有 published 字段
```
- Page 模型只有 `created` 和 `updated`，没有 `published`
- 任何访问 `page.hash` 的代码都会抛 `AttributeError`

### Bug 5: `Page.publish()` 直接修改私有属性 `_key_name` (models.py:170)
```python
self._key_name = self.path
self.put()
```
- 在 GAE Datastore 中，修改 `_key_name` 后 `put()` 会创建新实体，旧实体仍保留
- 这是设计如此（GAE key 不可变），但调用方必须显式清理旧实体
- 当前清理逻辑在 `PageHandler.post` 中，与 `publish()` 分离，容易遗漏

### Bug 6: `PageHandler.post` 保存与删除逻辑分散
- 新建页面：`form.save` → `publish`（创建 StaticContent）
- 编辑页面（路径不变）：`form.save` → `publish`（更新 StaticContent）
- 编辑页面（路径变更）：`form.save(instance=None)` → `publish`（创建新 StaticContent）→ `oldpage.remove()`（删旧实体 + 删旧 StaticContent）
- 删除页面：`PageDeleteHandler` → `page.remove()`
- 三套路径各自独立，应统一收口

---

## 修改方案

### 文件 1: `handlers.py`

#### 1a. 重写 `PageForm`
- **正则修复**: 改为 `r'^/([a-zA-Z0-9/_\.\-]+)?$'`，允许根路径 `/`，允许字母数字及常见路径字符，添加 `$` 锚定
- **`clean_path` 重写**: 接受当前编辑页面的旧路径参数，排除自身后检查重复
  ```python
  def __init__(self, *args, **kwargs):
      self._current_path = kwargs.pop('current_path', None)
      super(PageForm, self).__init__(*args, **kwargs)

  def clean_path(self):
      data = self.cleaned_data.get('path', '')
      if not data:
          raise forms.ValidationError("Path cannot be empty.")
      existing = models.Page.get_by_key_name(data)
      if existing and data != self._current_path:
          raise forms.ValidationError("The given path already exists.")
      return data
  ```

#### 1b. 重写 `PageHandler.post` — 统一保存流程
- 显式追踪 `old_path`（在 form 处理之前从原始 page 对象获取）
- 传 `current_path` 给 PageForm 以便 clean_path 排除自身
- 路径变更流程：先校验 → 创建新实体 → 发布新静态内容 → 删除旧实体 → 删除旧静态内容
- 对 oldpage 做 null-check
- 使用 `form.cleaned_data` 而非 `form._cleaned_data()`

```python
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
    if not form.is_valid():
        self.render_form(form)
        return

    page_obj = form.save(commit=False)
    page_obj.updated = datetime.datetime.now()
    page_obj.publish()

    if path_changed and old_path:
        old_page = models.Page.get_by_key_name(old_path)
        if old_page:
            old_page.remove()

    self.render_to_response("publishedpage.html", {'page': page_obj})
```

### 文件 2: `models.py`

#### 2a. 修复 `Page.hash` 属性
```python
@property
def hash(self):
    val = (self.path, self.body, self.updated)
    return hashlib.sha1(str(val)).hexdigest()
```

#### 2b. 加固 `Page.remove()`
- 在删除前先获取 path，避免实体删除后属性丢失的隐患
```python
def remove(self):
    if not self.is_saved():
        return
    path = self.path
    self.delete()
    generators.PageContentGenerator.generate_resource(self, path, action='delete')
```

### 文件 3: `tests.py`（新建）

编写回归测试覆盖以下场景：

1. **新建页面** — 正常路径（如 `/about`）
2. **新建页面路径冲突** — 路径已存在应校验失败
3. **编辑页面不改路径** — 内容更新，路径不变，不触发旧实体删除
4. **编辑页面改路径** — 旧实体被删，旧 StaticContent 被清，新 StaticContent 被创建
5. **根路径页面** — `/` 应能通过校验并保存
6. **嵌套路径** — `/a/b/c` 应能通过校验
7. **路径含特殊字符** — `/about-me`、`/my_page`、`/file.html` 应通过
8. **非法路径** — 空路径、不含前导 `/` 的路径应被拒绝
9. **Page.hash 可访问** — 不抛 AttributeError
10. **Page.remove 幂等** — 未保存的 page 调用 remove 不报错
11. **删除页面** — 实体和 StaticContent 都被清理

由于项目依赖 Google App Engine SDK（`google.appengine.ext.db`），测试需使用 GAE 的 testbed 或 mock。编写测试时使用 `google.appengine.ext.testbed` 提供的 datastore stub。

---

## 修改文件清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `handlers.py` | 修改 | 修复 PageForm 正则 + clean_path + PageHandler.post |
| `models.py` | 修改 | 修复 Page.hash + 加固 Page.remove |
| `tests.py` | 新建 | 回归测试 |

---

## 验证方式

1. 运行 `tests.py` 中全部测试用例
2. 手动检查：
   - `PageForm` 正则在 Python `re` 模块下对 `/`、`/about`、`/a/b/c`、`/about-me`、`/my.page` 均匹配
   - `PageForm` 正则在 `!@#`、空字符串、`no-slash` 上不匹配
   - `clean_path` 在新建时检测到重复路径报错，编辑时同一路径不报错
   - `PageHandler.post` 路径变更时 old_path 正确追踪且 oldpage 有 null-check
