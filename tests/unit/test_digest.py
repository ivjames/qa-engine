"""Unit tests for digest.py's semantic_skeleton / dom_shape_signature /
digest_hash."""

import hashlib

from digest import digest_hash, dom_shape_signature, semantic_skeleton

BASE_URL = "https://example.com/"


# ---------------------------------------------------------------------------
# semantic_skeleton: landmark / form / link retention + query stripping
# ---------------------------------------------------------------------------


def test_landmarks_form_controls_and_links_are_kept():
    html = """
    <html><body>
      <header><nav><a href="https://example.com/page?foo=bar&x=1#sec">Home</a></nav></header>
      <main>
        <h1>Welcome</h1>
        <form>
          <label for="email">Email</label>
          <input type="email" id="email" name="email" placeholder="you@example.com">
          <button type="submit">Submit</button>
        </form>
        <a href="/about?ref=nav">About</a>
      </main>
      <footer><p>copyright</p></footer>
    </body></html>
    """
    skeleton = semantic_skeleton(html, BASE_URL)

    for tag_open in ("<header>", "<nav>", "<main>", "<form>", "<footer>"):
        assert tag_open in skeleton

    assert "<h1>" in skeleton and "Welcome" in skeleton

    # form controls with their attrs
    assert 'type="email"' in skeleton
    assert 'name="email"' in skeleton
    assert 'id="email"' in skeleton
    assert 'placeholder="you@example.com"' in skeleton
    assert 'for="email"' in skeleton

    # absolute href: query + fragment stripped, path kept
    assert 'href="https://example.com/page"' in skeleton
    # relative href resolved against base_url, then stripped
    assert 'href="https://example.com/about"' in skeleton

    assert "foo=bar" not in skeleton
    assert "#sec" not in skeleton
    assert "ref=nav" not in skeleton


def test_link_query_and_fragment_always_stripped():
    html = '<body><main><a href="/x/y?a=1&b=2#frag">link</a></main></body>'
    skeleton = semantic_skeleton(html, "https://site.test/")
    assert 'href="https://site.test/x/y"' in skeleton
    assert "?" not in skeleton
    assert "#frag" not in skeleton


def test_img_alt_and_src_basename_kept():
    html = '<body><main><img src="https://cdn.example.com/assets/photo123.jpg?w=200" alt="A dog"></main></body>'
    skeleton = semantic_skeleton(html, BASE_URL)
    assert 'alt="A dog"' in skeleton
    assert "photo123.jpg" in skeleton
    # only the basename, not the full url
    assert "cdn.example.com" not in skeleton


def test_role_and_aria_attributes_are_kept_on_any_element():
    html = '<body><div role="alert" aria-live="polite">warn</div></body>'
    skeleton = semantic_skeleton(html, BASE_URL)
    assert 'role="alert"' in skeleton
    assert 'aria-live="polite"' in skeleton


# ---------------------------------------------------------------------------
# Dropped content
# ---------------------------------------------------------------------------


def test_script_style_and_other_noise_are_dropped():
    html = """
    <html><body>
      <!-- a secret comment -->
      <script>doEvilThings();</script>
      <style>.a { color: red; }</style>
      <meta charset="utf-8">
      <link rel="stylesheet" href="theme.css">
      <noscript>enable javascript please</noscript>
      <iframe src="https://ads.example.com">ad iframe body text</iframe>
      <main><h1>Real Title</h1></main>
    </body></html>
    """
    skeleton = semantic_skeleton(html, BASE_URL)

    for banned in (
        "doEvilThings",
        "color: red",
        "secret comment",
        "theme.css",
        "enable javascript",
        "ad iframe body text",
        "charset",
    ):
        assert banned not in skeleton

    assert "Real Title" in skeleton


# ---------------------------------------------------------------------------
# Repeated sibling collapse
# ---------------------------------------------------------------------------


def test_repeated_cards_collapse_to_one_plus_marker():
    card = '<div class="card"><h3>Widget</h3><a href="/widget">View</a></div>'
    html = "<html><body><main>" + (card * 20) + "</main></body></html>"

    skeleton = semantic_skeleton(html, BASE_URL)

    assert skeleton.count("Widget") == 1
    assert "<!-- x19 more -->" in skeleton
    # collapsing must make the skeleton much smaller than the raw input
    assert len(skeleton) < len(html) * 0.5


def test_collapse_only_groups_consecutive_matching_siblings():
    html = (
        "<html><body><main>"
        '<div class="card"><h3>A</h3></div>'
        '<div class="card"><h3>A</h3></div>'
        '<div class="other"><h3>B</h3></div>'
        '<div class="card"><h3>A</h3></div>'
        "</main></body></html>"
    )
    skeleton = semantic_skeleton(html, BASE_URL)
    # first run of 2 "card" divs -> 1 kept + marker for 1 more
    assert "<!-- x1 more -->" in skeleton
    # the "other" div and the trailing lone "card" div are not part of that
    # run, so they still show up on their own
    assert skeleton.count("<h3>") == 3  # first card, "other", trailing card


# ---------------------------------------------------------------------------
# Text truncation
# ---------------------------------------------------------------------------


def test_long_text_is_truncated_to_120_chars():
    long_text = "x" * 500
    html = f"<html><body><main><h1>{long_text}</h1></main></body></html>"
    skeleton = semantic_skeleton(html, BASE_URL)
    # find the line with the heading and check no run of the filler exceeds 120
    assert ("x" * 121) not in skeleton
    assert ("x" * 120) in skeleton


# ---------------------------------------------------------------------------
# dom_shape_signature
# ---------------------------------------------------------------------------


def test_dom_shape_signature_same_for_same_layout_different_text():
    html_a = """
    <html><body><header><nav class="top"><a href="/a">A</a></nav></header>
    <main><h1>Product Alpha</h1><p>Alpha description text goes here.</p></main>
    <footer>Alpha Co</footer></body></html>
    """
    html_b = """
    <html><body><header><nav class="top"><a href="/xyz">Different label</a></nav></header>
    <main><h1>Something Else Entirely</h1><p>Totally unrelated paragraph content.</p></main>
    <footer>Beta Corp</footer></body></html>
    """
    assert dom_shape_signature(html_a) == dom_shape_signature(html_b)


def test_dom_shape_signature_differs_for_different_layout():
    html_a = "<html><body><main><h1>T</h1><p>x</p></main></body></html>"
    html_c = (
        "<html><body><main><h1>T</h1><div>extra</div><p>x</p>"
        "<section>more</section></main></body></html>"
    )
    assert dom_shape_signature(html_a) != dom_shape_signature(html_c)


def test_dom_shape_signature_ignores_class_free_attrs_like_href_and_value():
    html_a = '<body><main><a href="/one" title="t1">L</a></main></body>'
    html_b = '<body><main><a href="/two" title="t2">M</a></main></body>'
    assert dom_shape_signature(html_a) == dom_shape_signature(html_b)


def test_dom_shape_signature_is_sha256_hex():
    sig = dom_shape_signature("<body><main><h1>Hi</h1></main></body>")
    assert isinstance(sig, str)
    assert len(sig) == 64
    int(sig, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# digest_hash
# ---------------------------------------------------------------------------


def test_digest_hash_changes_when_visible_text_changes():
    html_a = "<html><body><main><h1>Version One</h1></main></body></html>"
    html_b = "<html><body><main><h1>Version Two</h1></main></body></html>"

    skeleton_a = semantic_skeleton(html_a, BASE_URL)
    skeleton_b = semantic_skeleton(html_b, BASE_URL)

    assert skeleton_a != skeleton_b
    assert digest_hash(skeleton_a) != digest_hash(skeleton_b)


def test_digest_hash_is_sha256_of_the_exact_string():
    s = "some skeleton text\nwith lines"
    assert digest_hash(s) == hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_digest_hash_empty_is_stable():
    assert digest_hash("") == hashlib.sha256(b"").hexdigest()
    assert digest_hash(None) == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_semantic_skeleton_is_deterministic():
    html = """
    <html><body><header><nav>
      <a href="/a?x=1">A</a><a href="/b?y=2">B</a><a href="/c?z=3">C</a>
    </nav></header>
    <main><h2>Section</h2><img src="/img/pic.png?v=2" alt="pic"></main>
    </body></html>
    """
    first = semantic_skeleton(html, BASE_URL)
    second = semantic_skeleton(html, BASE_URL)
    assert first == second
    assert digest_hash(first) == digest_hash(second)


def test_dom_shape_signature_is_deterministic():
    html = "<html><body><main><h1>Hi</h1><p>text</p></main></body></html>"
    assert dom_shape_signature(html) == dom_shape_signature(html)


# ---------------------------------------------------------------------------
# Robustness: malformed / empty / None input
# ---------------------------------------------------------------------------


def test_malformed_html_does_not_raise():
    malformed = "<html><body><div class='unclosed'><p>Oops<main><h1>Broken<footer>"
    skeleton = semantic_skeleton(malformed, BASE_URL)
    assert isinstance(skeleton, str)

    sig = dom_shape_signature(malformed)
    assert isinstance(sig, str)
    assert len(sig) == 64


def test_deeply_broken_markup_does_not_raise():
    malformed = "<<<not really html>>> </div></div></span" * 50
    skeleton = semantic_skeleton(malformed, BASE_URL)
    assert isinstance(skeleton, str)
    sig = dom_shape_signature(malformed)
    assert isinstance(sig, str)


def test_empty_and_none_html_gives_empty_skeleton():
    assert semantic_skeleton("", BASE_URL) == ""
    assert semantic_skeleton(None, BASE_URL) == ""
    assert semantic_skeleton("   \n\t  ", BASE_URL) == ""


def test_empty_and_none_html_gives_stable_shape_signature():
    empty_sig = hashlib.sha256(b"").hexdigest()
    assert dom_shape_signature("") == empty_sig
    assert dom_shape_signature(None) == empty_sig


# ---------------------------------------------------------------------------
# Token-frugality target on a realistic ~100KB synthetic page
# ---------------------------------------------------------------------------


def _build_synthetic_page(n_cards: int = 120) -> str:
    cards = []
    for i in range(n_cards):
        cards.append(
            f"""
            <div class="product-card">
              <div class="product-card__media">
                <img src="https://cdn.example.com/img/product-{i}.jpg" alt="Product {i} photo">
              </div>
              <div class="product-card__body">
                <h3>Product {i} Deluxe Widget Model {i}</h3>
                <p class="desc">This is a long marketing description for product {i}
                   that goes on for a while about quality, durability, and craftsmanship
                   in ways that add many bytes but very little unique structural signal.</p>
                <a href="/products/{i}?utm_source=list&utm_campaign=summer#reviews">View details</a>
                <button type="button" aria-label="Add product {i} to cart">Add to cart</button>
              </div>
            </div>
            """
        )
    cards_html = "".join(cards)

    return f"""<!doctype html>
    <html><head><meta charset="utf-8"><title>Shop</title>
      <link rel="stylesheet" href="/styles/main.css">
      <style>body {{ margin: 0; }} .product-card {{ border: 1px solid #eee; }}</style>
      <script>window.__DATA__ = {{}};</script>
    </head>
    <body>
      <header>
        <nav aria-label="Main navigation">
          <a href="/">Home</a>
          <a href="/shop?sort=popular">Shop</a>
          <a href="/about">About</a>
        </nav>
      </header>
      <main>
        <h1>Our Products</h1>
        <section aria-label="Product grid">
          {cards_html}
        </section>
      </main>
      <footer>
        <form>
          <label for="newsletter">Subscribe</label>
          <input type="email" id="newsletter" name="newsletter" placeholder="you@example.com">
          <button type="submit">Join</button>
        </form>
        <p>&copy; 2026 Example Shop. All rights reserved.</p>
      </footer>
      <script>console.log("loaded");</script>
    </body></html>"""


def test_synthetic_100kb_page_skeleton_is_token_frugal():
    html = _build_synthetic_page(n_cards=120)
    # sanity: this really is a ~100KB-scale synthetic page
    assert len(html) > 80_000

    skeleton = semantic_skeleton(html, "https://shop.example.com/")

    # repeated product cards must collapse to one instance + a marker
    assert "<!-- x119 more -->" in skeleton
    assert skeleton.count("product-") <= 2  # first card's img src/id-ish text only

    # the whole point: skeleton should be dramatically smaller than the page,
    # comfortably inside a small-KB budget regardless of how many cards exist.
    assert len(skeleton) < 4000
    assert len(skeleton.encode("utf-8")) < 12_000

    # determinism holds even at this size
    assert semantic_skeleton(html, "https://shop.example.com/") == skeleton
