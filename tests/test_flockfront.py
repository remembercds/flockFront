"""Unit tests for flockFront's pure logic — no network, no Cloudflare account."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flockFront as ff  # noqa: E402

# --- validate_domain -------------------------------------------------------

@pytest.mark.parametrize(
    "domain,expected",
    [
        ("example.com", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        ("sub.example.co.uk", "sub.example.co.uk"),
        ("my-shop.example.com", "my-shop.example.com"),
    ],
)
def test_validate_domain_accepts_and_lowercases(domain, expected):
    assert ff.validate_domain(domain) == expected


@pytest.mark.parametrize(
    "domain",
    ["localhost", "", "-bad.com", "bad-.com", "exa mple.com", "http://example.com", "example..com"],
)
def test_validate_domain_rejects_junk(domain):
    with pytest.raises(ValueError):
        ff.validate_domain(domain)


# --- script_name_for -------------------------------------------------------

def test_script_name_short_domain_is_readable():
    assert ff.script_name_for("example.com") == "flockfront-example-com"


def test_script_name_respects_cloudflare_length_cap():
    long_domain = ("a" * 80) + ".com"
    assert len(ff.script_name_for(long_domain)) <= ff.MAX_SCRIPT_NAME


def test_long_domains_sharing_a_prefix_do_not_collide():
    """Regression: plain [:63] truncation made these one script, so deploying
    the second silently overwrote the first and deleting either killed both."""
    a = "national-association-of-independent-financial-advisors-group.com"
    b = "national-association-of-independent-financial-advisors-group.net"
    assert ff.script_name_for(a) != ff.script_name_for(b)


def test_script_name_is_deterministic():
    assert ff.script_name_for("example.com") == ff.script_name_for("example.com")


def test_truncated_script_name_has_no_trailing_dash():
    # 'flockfront-' + 44 chars lands the cut exactly on a '-'
    domain = ("x" * 44) + "-verylongtail.com"
    name = ff.script_name_for(domain)
    assert "--" not in name and not name.endswith("-")


# --- zone_candidates -------------------------------------------------------

def test_zone_candidates_walks_up_to_the_registrable_domain():
    assert ff.zone_candidates("shop.example.com") == ["shop.example.com", "example.com"]


def test_zone_candidates_apex_domain_is_itself():
    assert ff.zone_candidates("example.com") == ["example.com"]


def test_zone_candidates_never_yields_a_bare_tld():
    for candidate in ff.zone_candidates("a.b.c.example.com"):
        assert "." in candidate


# --- render_template -------------------------------------------------------

def test_render_template_substitutes_placeholders():
    assert ff.render_template("Hi $NAME", {"NAME": "Ada"}) == "Hi Ada"


def test_render_template_leaves_literal_dollar_amounts_alone():
    """string.Template.substitute() raised ValueError here, so any template
    mentioning a price would have broken every render."""
    out = ff.render_template("Lunch from $12.50 at $NAME", {"NAME": "Cafe"})
    assert out == "Lunch from $12.50 at Cafe"


def test_render_template_rejects_unknown_placeholder():
    with pytest.raises(KeyError):
        ff.render_template("$MYSTERY", {"NAME": "Ada"})


# --- render_site_html ------------------------------------------------------

@pytest.mark.parametrize("industry", sorted(ff.INDUSTRY_TAGLINES))
def test_every_template_renders(industry):
    html = ff.render_site_html(industry, "sunrise-wealth.com")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "</html>" in html
    assert "Sunrise Wealth" in html
    assert not ff.PLACEHOLDER_RE.search(html), "unsubstituted $PLACEHOLDER left in output"


@pytest.mark.parametrize("industry", sorted(ff.INDUSTRY_TAGLINES))
def test_templates_have_a_tagline_for_every_industry(industry):
    assert (ff.TEMPLATES_DIR / f"{industry}.html").is_file()


def test_render_site_html_escapes_business_name():
    html = ff.render_site_html("finance", "a-b.com")
    assert "A B" in html


# --- business_name_from_domain --------------------------------------------

@pytest.mark.parametrize(
    "domain,expected",
    [
        ("sunrise-wealth.com", "Sunrise Wealth"),
        ("acme.io", "Acme"),
        ("my_great_firm.net", "My Great Firm"),
    ],
)
def test_business_name_from_domain(domain, expected):
    assert ff.business_name_from_domain(domain) == expected


# --- clean_ai_html ---------------------------------------------------------

COMPLETE = "<!doctype html><html><body>hi</body></html>"


def test_clean_ai_html_strips_markdown_fences():
    assert ff.clean_ai_html(f"```html\n{COMPLETE}\n```") == COMPLETE


def test_clean_ai_html_passes_complete_document():
    assert ff.clean_ai_html(COMPLETE) == COMPLETE


def test_clean_ai_html_rejects_non_html():
    with pytest.raises(ff.AIGenerationError):
        ff.clean_ai_html("Sure! Here's a website for you.")


def test_clean_ai_html_rejects_truncated_document():
    """Regression: output cut off by the token limit still contains '<html',
    so it used to pass validation and deploy as a finished page."""
    truncated = "<!doctype html><html><body><div class='hero'><h1>Acme"
    with pytest.raises(ff.AIGenerationError, match="truncated"):
        ff.clean_ai_html(truncated)


# --- render_worker_module --------------------------------------------------

def test_worker_module_embeds_html_as_valid_json():
    html = '<p class="x">He said "hi" & left</p>\n<script>a < b</script>'
    module = ff.render_worker_module(html)
    literal = module.split("const html = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(literal) == html


def test_worker_module_sets_html_content_type():
    assert "text/html; charset=UTF-8" in ff.render_worker_module("<html></html>")


# --- dedupe ----------------------------------------------------------------

def test_dedupe_preserves_order_and_drops_repeats():
    assert ff.dedupe(["b.com", "a.com", "b.com"]) == ["b.com", "a.com"]


def test_dedupe_is_case_insensitive():
    assert ff.dedupe(["A.com", "a.com"]) == ["A.com"]


# --- slugify ---------------------------------------------------------------

def test_slugify_replaces_dots_and_lowercases():
    assert ff.slugify("Example.COM") == "example-com"
