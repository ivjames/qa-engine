"""Semantic skeleton extraction.

Turns a raw HTML page into a compact, deterministic, token-frugal pseudo-HTML
"skeleton" that preserves the parts of the page an LLM needs to reason about
QA (landmarks, headings, form controls, links, images, tables, ARIA) while
dropping everything else (scripts, styles, svg innards, comments, meta,
link, noscript/iframe contents).

Also provides two hashing helpers:

- ``dom_shape_signature`` — a structural fingerprint (tag + sorted classes,
  document order, depth/element capped) that is stable across pages sharing
  the same layout regardless of their text content.
- ``digest_hash`` — a content-sensitive hash of the skeleton text itself.

Everything here is pure and deterministic: no dict/set iteration is allowed
to leak into output ordering, and no id()-based or wall-clock-based values
are ever emitted.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tags whose entire subtree is dropped (nothing beneath them is ever kept).
_DROP_SUBTREE_TAGS = frozenset(
    {"script", "style", "svg", "noscript", "iframe", "meta", "link", "template"}
)

# Landmark / structural tags kept as container nodes in the skeleton.
_LANDMARK_TAGS = frozenset(
    {"header", "nav", "main", "footer", "aside", "section", "article", "form"}
)

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

_FORM_CONTROL_TAGS = frozenset({"input", "button", "select", "textarea", "label"})

# Table structure only (thead/tbody/tr/th/td), per spec.
_TABLE_TAGS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "th", "td"})

# Attributes worth keeping on form controls, in a fixed, deterministic order.
# "required" is boolean (rendered bare, no value); role/aria-label/
# aria-labelledby are covered by the generic role/aria-* passthrough below.
_FORM_ATTR_ORDER = (
    "type",
    "name",
    "id",
    "placeholder",
    "for",
    "alt",
    "value",
)

_TEXT_TRUNCATE_LEN = 120
_MAX_DEPTH_FOR_SIGNATURE = 12
_MAX_ELEMENTS_FOR_SIGNATURE = 400


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _has_aria_or_role(tag: Tag) -> bool:
    if not tag.attrs:
        return False
    if "role" in tag.attrs:
        return True
    return any(k.lower().startswith("aria-") for k in tag.attrs.keys())


def _class_sig(tag: Tag) -> tuple:
    """Sorted, deterministic class list for signature/collapse comparisons."""
    classes = tag.attrs.get("class") if tag.attrs else None
    if not classes:
        return tuple()
    if isinstance(classes, str):
        classes = classes.split()
    return tuple(sorted(str(c) for c in classes))


def _clean_text(node_text: str) -> str:
    if not node_text:
        return ""
    collapsed = " ".join(node_text.split())
    if len(collapsed) > _TEXT_TRUNCATE_LEN:
        collapsed = collapsed[:_TEXT_TRUNCATE_LEN]
    return collapsed


def _collect_own_text(tag: Tag, parts: list) -> None:
    """Gather text belonging to ``tag``'s own displayed line: all descendant
    text EXCEPT text inside a nested tag that is itself independently
    "relevant" (that nested tag gets its own skeleton line, so its text isn't
    duplicated here). Lets inline formatting (span/b/em/...) contribute its
    text to the enclosing heading/link/etc. while avoiding duplication with
    nested landmarks/headings/links/etc."""
    for child in tag.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in _DROP_SUBTREE_TAGS:
            continue
        if _relevant(child):
            continue
        _collect_own_text(child, parts)


def _direct_text(tag: Tag) -> str:
    """This tag's own text (see ``_collect_own_text``), cleaned/truncated."""
    parts: list = []
    _collect_own_text(tag, parts)
    return _clean_text(" ".join(parts))


def _strip_href(href: str, base_url: str) -> str:
    """Resolve href against base_url, then strip query string and fragment."""
    if href is None:
        return ""
    try:
        resolved = urljoin(base_url or "", href)
    except ValueError:
        resolved = href
    try:
        parts = urlsplit(resolved)
        stripped = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return stripped
    except ValueError:
        return resolved


def _src_basename(src: str) -> str:
    if not src:
        return ""
    trimmed = src.split("?", 1)[0].split("#", 1)[0]
    trimmed = trimmed.rstrip("/")
    if "/" in trimmed:
        return trimmed.rsplit("/", 1)[-1]
    return trimmed


def _sorted_attr_items(attrs: dict) -> list:
    """Deterministic (key, value)-as-string pairs, sorted by key, for any
    aria-*/role passthrough attributes not otherwise handled explicitly."""
    out = []
    if not attrs:
        return out
    for k in sorted(attrs.keys()):
        v = attrs[k]
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        out.append((k, "" if v is None else str(v)))
    return out


# ---------------------------------------------------------------------------
# semantic_skeleton
# ---------------------------------------------------------------------------


def _relevant(tag: Tag) -> bool:
    """Whether this element itself (not its children) is worth a line in the
    skeleton."""
    name = (tag.name or "").lower()
    if name in _LANDMARK_TAGS:
        return True
    if name in _HEADING_TAGS:
        return True
    if name in _FORM_CONTROL_TAGS:
        return True
    if name == "a" and tag.attrs.get("href") is not None:
        return True
    if name == "img":
        return True
    if name in _TABLE_TAGS:
        return True
    if _has_aria_or_role(tag):
        return True
    return False


def _render_attrs_for(tag: Tag, base_url: str) -> str:
    name = (tag.name or "").lower()
    bits = []

    if name in _FORM_CONTROL_TAGS:
        for key in _FORM_ATTR_ORDER:
            if key == "value":
                # only surface value for buttons (per spec: "value-if-button")
                if name != "button":
                    continue
                val = tag.attrs.get("value")
                if val:
                    bits.append('value="%s"' % _clean_text(str(val)))
                continue
            val = tag.attrs.get(key)
            if val:
                bits.append('%s="%s"' % (key, _clean_text(str(val))))
        if tag.attrs.get("required") is not None:
            bits.append("required")
        # aria-* passthrough, sorted, on top of the fixed fields above
        for k, v in _sorted_attr_items(tag.attrs):
            if k.lower().startswith("aria-"):
                bits.append('%s="%s"' % (k, _clean_text(v)))

    elif name == "a":
        href = _strip_href(tag.attrs.get("href", ""), base_url)
        bits.append('href="%s"' % href)

    elif name == "img":
        alt = tag.attrs.get("alt", "")
        bits.append('alt="%s"' % _clean_text(str(alt)))
        src = tag.attrs.get("src")
        if src:
            bits.append('src="%s"' % _src_basename(str(src)))

    # role / aria-* on ANY element (landmarks, headings, tables, etc.) that
    # isn't already a form control (handled above to avoid duplication).
    if name not in _FORM_CONTROL_TAGS:
        role = tag.attrs.get("role")
        if role:
            bits.append('role="%s"' % _clean_text(str(role)))
        for k, v in _sorted_attr_items(tag.attrs):
            if k.lower().startswith("aria-"):
                bits.append('%s="%s"' % (k, _clean_text(v)))

    return (" " + " ".join(bits)) if bits else ""


def _render_line(tag: Tag, base_url: str, depth: int) -> str:
    name = (tag.name or "").lower()
    attrs = _render_attrs_for(tag, base_url)
    text = _direct_text(tag)
    indent = "  " * depth
    line = "%s<%s%s>" % (indent, name, attrs)
    if text:
        line += " %s" % text
    return line


def _iter_kept_children(tag: Tag):
    """Yield child Tag nodes worth walking into (skip dropped subtrees,
    comments, and plain strings that carry no independent relevance)."""
    for child in tag.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue
        if (child.name or "").lower() in _DROP_SUBTREE_TAGS:
            continue
        yield child


def _walk_collect(tag: Tag, base_url: str, depth: int, out: list) -> None:
    """Depth-first walk emitting skeleton lines with sibling-collapse applied
    at each level."""
    children = list(_iter_kept_children(tag))

    # Group consecutive siblings by (tag, class-signature) to collapse
    # repeated structures, but only among children that are themselves
    # "relevant" landmarks/repeatable containers OR any tag at all --
    # collapsing must work for any repeated sibling shape (e.g. plain
    # <div class="card">), not just tags in _relevant(). So track run-length
    # over ALL kept children by (tag name, class signature); a run only
    # collapses if length > 1.
    i = 0
    n = len(children)
    while i < n:
        child = children[i]
        sig = ((child.name or "").lower(), _class_sig(child))
        j = i + 1
        while j < n:
            other = children[j]
            other_sig = ((other.name or "").lower(), _class_sig(other))
            if other_sig != sig:
                break
            j += 1
        run_len = j - i

        _emit_subtree(child, base_url, depth, out)
        if run_len > 1:
            out.append("%s<!-- x%d more -->" % ("  " * depth, run_len - 1))
        i = j


def _emit_subtree(tag: Tag, base_url: str, depth: int, out: list) -> None:
    is_relevant = _relevant(tag)
    if is_relevant:
        out.append(_render_line(tag, base_url, depth))
        next_depth = depth + 1
    else:
        next_depth = depth
    _walk_collect(tag, base_url, next_depth, out)


def semantic_skeleton(html: str, base_url: str = "") -> str:
    """Parse ``html`` and return a compact, deterministic pseudo-HTML
    skeleton: landmarks, headings, form controls, links, images, tables, and
    any ARIA/role-bearing elements, with repeated sibling structures
    collapsed and text truncated. Malformed or empty input never raises."""
    if not html or not isinstance(html, str) or not html.strip():
        return ""

    base_url = base_url or ""

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return ""

    root = soup.body if soup.body is not None else soup

    out: list = []
    try:
        _walk_collect(root, base_url, 0, out)
    except Exception:
        return ""

    return "\n".join(out)


# ---------------------------------------------------------------------------
# dom_shape_signature
# ---------------------------------------------------------------------------


def _shape_walk(tag: Tag, depth: int, counter: list, out: list) -> None:
    if counter[0] >= _MAX_ELEMENTS_FOR_SIGNATURE:
        return
    if depth > _MAX_DEPTH_FOR_SIGNATURE:
        return

    for child in tag.children:
        if counter[0] >= _MAX_ELEMENTS_FOR_SIGNATURE:
            return
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in _DROP_SUBTREE_TAGS:
            continue

        counter[0] += 1
        classes = _class_sig(child)
        out.append("%s|%s" % (name, ",".join(classes)))

        if counter[0] < _MAX_ELEMENTS_FOR_SIGNATURE:
            _shape_walk(child, depth + 1, counter, out)


def dom_shape_signature(html: str) -> str:
    """sha256 hex digest of the page's STRUCTURE ONLY: tag names + sorted
    class attributes walked in document order, all text/href/src/value
    stripped. Depth-limited to 12 levels, capped at the first 400 elements.
    Two pages with identical layout but different content/text produce the
    same signature."""
    if not html or not isinstance(html, str) or not html.strip():
        return hashlib.sha256(b"").hexdigest()

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return hashlib.sha256(b"").hexdigest()

    root = soup.body if soup.body is not None else soup

    out: list = []
    counter = [0]
    try:
        _shape_walk(root, 0, counter, out)
    except Exception:
        return hashlib.sha256(b"").hexdigest()

    structure_text = "\n".join(out)
    return hashlib.sha256(structure_text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# digest_hash
# ---------------------------------------------------------------------------


def digest_hash(skeleton: str) -> str:
    """sha256 hex digest of the skeleton string itself (text included --
    content changes must change this hash)."""
    if skeleton is None:
        skeleton = ""
    return hashlib.sha256(skeleton.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _SAMPLE_HTML = """
    <html>
    <head><title>t</title><style>.x{color:red}</style></head>
    <body>
      <header><nav aria-label="Main"><a href="/home?x=1#frag">Home</a></nav></header>
      <main>
        <h1>Products</h1>
        <section class="grid">
          <div class="card"><h3>Widget A</h3><a href="/p/1">View</a></div>
          <div class="card"><h3>Widget B</h3><a href="/p/2">View</a></div>
          <div class="card"><h3>Widget C</h3><a href="/p/3">View</a></div>
          <div class="card"><h3>Widget D</h3><a href="/p/4">View</a></div>
        </section>
        <form>
          <label for="email">Email</label>
          <input type="email" id="email" name="email" placeholder="you@x.com" required>
          <button type="submit">Go</button>
        </form>
      </main>
      <footer><p>copyright</p></footer>
      <script>console.log('nope')</script>
      <!-- a comment -->
    </body>
    </html>
    """

    skeleton = semantic_skeleton(_SAMPLE_HTML, base_url="https://example.com/")
    shape_sig = dom_shape_signature(_SAMPLE_HTML)
    content_hash = digest_hash(skeleton)

    print(skeleton)
    print("---")
    print("dom_shape_signature:", shape_sig)
    print("digest_hash:", content_hash)

    assert "<script" not in skeleton
    assert "color:red" not in skeleton
    assert "a comment" not in skeleton
    # 4 repeated .card divs collapse: only the first card's kept descendants
    # (its <h3> + <a>) show up, the rest become a single marker line.
    assert skeleton.count("Widget A") == 1
    assert "Widget B" not in skeleton
    assert "<!-- x3 more -->" in skeleton
    # href normalized: resolved against base_url, query+fragment stripped.
    assert 'href="https://example.com/home"' in skeleton
    # boolean `required` preserved on the form control.
    assert "required" in skeleton
    assert 'for="email"' in skeleton
    # 2-space indent, as specced.
    assert any(line.startswith("  <") for line in skeleton.splitlines())

    # Same layout, different text/content -> identical structural signature.
    other_html = _SAMPLE_HTML.replace("Widget A", "Totally Different Product Name")
    assert dom_shape_signature(other_html) == shape_sig
    # But the content hash must change since the skeleton text changed.
    other_skeleton = semantic_skeleton(other_html, base_url="https://example.com/")
    assert digest_hash(other_skeleton) != content_hash

    print("OK")
