#!/usr/bin/env python3
"""
flockFront.py — Spin up a themed business website on Cloudflare Workers, for
one or more domains you already own and manage in Cloudflare.

Pick an industry template with -i/--industry (finance, healthcare, education,
legal, real_estate, restaurant, fitness). The template's business name, page
titles, and contact addresses are all generated from the domain you pass in.

Instead of a boilerplate template, you can have an AI model design the page:
pass --ai claude or --ai gemini with an API key, and flockFront asks the
model to generate a full HTML page for the business instead of using the
static templates.

Prerequisites (one-time, manual):
  1. A Cloudflare API token with:
       - Zone / Zone / Read
       - Zone / Workers Routes / Edit      (covers Workers Custom Domains)
       - Account / Workers Scripts / Edit
     Create one at: https://dash.cloudflare.com/profile/api-tokens
  2. (Optional but recommended) The domain already added as a zone in your
     Cloudflare account with its nameservers pointed at Cloudflare. If the
     domain isn't a Cloudflare zone yet, flockFront falls back to publishing
     on a *.workers.dev subdomain so you still get a live URL immediately.
  3. (Only if using --ai) An API key for the chosen provider:
       - Claude:  ANTHROPIC_API_KEY (or --ai-key), and `pip install anthropic`
       - Gemini:  GEMINI_API_KEY (or --ai-key)

Usage:
  export CLOUDFLARE_API_TOKEN=xxxxx
  export CLOUDFLARE_ACCOUNT_ID=xxxxx   # optional if your token sees exactly one account
  python flockFront.py example.com --industry finance
  python flockFront.py example.com -i healthcare
  python flockFront.py a.com b.com c.com -i fitness         # multiple domains
  python flockFront.py --domains-file domains.txt -i legal  # domains from a file
  python flockFront.py example.com --ai claude --ai-key sk-ant-...
  python flockFront.py example.com --ai gemini               # uses GEMINI_API_KEY
  python flockFront.py example.com --dry-run -i legal        # render locally, skip Cloudflare
  python flockFront.py a.com b.com --concurrency 8           # deploy 8 domains in parallel
  python flockFront.py --list                                # list every flockFront deployment
  python flockFront.py --delete example.com other.com        # tear down deployment(s)
  python flockFront.py --delete example.com --dry-run        # show what --delete would remove

Output:
  Prints the live URL for each domain once its site is deployed (or the local
  file:// path with --dry-run).
"""

import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://api.cloudflare.com/client/v4"
TEMPLATES_DIR = Path(__file__).parent / "templates"
FLOCKFRONT_PREFIX = "flockfront-"

INDUSTRY_TAGLINES = {
    "finance": "Your trusted partner in financial growth.",
    "healthcare": "Compassionate care, close to home.",
    "education": "Empowering minds, building futures.",
    "legal": "Experienced counsel, straightforward advice.",
    "real_estate": "Local expertise for every move you make.",
    "restaurant": "Great food, made for gathering.",
    "fitness": "Real coaching. Real community. Real results.",
}

CLAUDE_MODEL = "claude-opus-5"
GEMINI_MODEL = "gemini-3.5-flash"

BANNER = r"""
              ,;;;;;;;,
            ;;;;;;;;;;;;;
           ;;;()    ()~;;
            ;;(   ..   )~;;
             ';;;;;;;;;;;'
               )   ||   (
              (____||____)

                flockFront
     ~ herding your domain into a website ~
"""


def print_banner():
    print(BANNER, file=sys.stderr)


class CloudflareError(Exception):
    pass


class AIGenerationError(Exception):
    pass


REQUEST_TIMEOUT = (10, 60)  # (connect, read) seconds
RETRY_STATUSES = (429, 500, 502, 503, 504)


def build_session(retries=3):
    """A Session that retries transient Cloudflare failures and reuses connections."""
    policy = Retry(
        total=retries,
        backoff_factor=1.0,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset(["GET", "PUT", "POST", "DELETE"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=policy))
    return session


# Shared by default so the per-domain loop reuses connections. Threads each get
# their own session (see deploy_all) since retry state isn't thread-safe.
SESSION = build_session()


def cf_request_body(method, path, token, session=None, **kwargs):
    """Like cf_request, but returns the whole envelope (for result_info)."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    http = session or SESSION
    resp = http.request(method, f"{API_BASE}{path}", headers=headers, **kwargs)
    try:
        body = resp.json()
    except ValueError as e:
        resp.raise_for_status()
        raise CloudflareError(f"Unexpected non-JSON response from {path}") from e

    if not body.get("success", False):
        errors = body.get("errors", [])
        msg = "; ".join(e.get("message", str(e)) for e in errors) or resp.text
        raise CloudflareError(f"Cloudflare API error ({method} {path}): {msg}")

    return body


def cf_request(method, path, token, session=None, **kwargs):
    return cf_request_body(method, path, token, session=session, **kwargs).get("result")


def validate_domain(domain):
    pattern = r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$"
    if not re.match(pattern, domain):
        raise ValueError(f"'{domain}' doesn't look like a valid domain name")
    return domain.lower()


def slugify(domain):
    return re.sub(r"[^a-z0-9-]", "-", domain.lower())


MAX_SCRIPT_NAME = 63


def script_name_for(domain):
    """Worker script name for a domain, unique even when the slug is too long.

    Cloudflare caps script names at 63 characters. Plain truncation makes two
    different long domains collide onto one script, so the second deploy
    silently overwrites the first (and deleting either tears down both). When
    the name doesn't fit, truncate further and append a digest of the full
    domain so distinct domains keep distinct scripts.
    """
    name = f"{FLOCKFRONT_PREFIX}{slugify(domain)}"
    if len(name) <= MAX_SCRIPT_NAME:
        return name

    digest = hashlib.sha256(domain.encode("utf-8")).hexdigest()[:8]
    suffix = f"-{digest}"
    return name[: MAX_SCRIPT_NAME - len(suffix)].rstrip("-") + suffix


PLACEHOLDER_RE = re.compile(r"\$([A-Z][A-Z0-9_]*)")


def render_template(text, values):
    """Substitute $PLACEHOLDER tokens, leaving literal '$' (e.g. prices) alone.

    string.Template.substitute() raises on any bare '$' in the source, which
    would break the moment a template mentions a dollar amount. Only uppercase
    tokens are treated as placeholders; an unknown one is a template bug, so
    it raises rather than silently rendering as-is.
    """

    def replace(match):
        key = match.group(1)
        if key not in values:
            raise KeyError(f"unknown template placeholder ${key}")
        return values[key]

    return PLACEHOLDER_RE.sub(replace, text)


def business_name_from_domain(domain):
    base = domain.split(".")[0]
    words = re.split(r"[-_]+", base)
    return " ".join(w.capitalize() for w in words if w)


def render_site_html(industry, domain):
    template_path = TEMPLATES_DIR / f"{industry}.html"
    return render_template(
        template_path.read_text(encoding="utf-8"),
        {
            "BUSINESS_NAME": escape(business_name_from_domain(domain)),
            "DOMAIN": escape(domain),
            "TAGLINE": escape(INDUSTRY_TAGLINES[industry]),
            "YEAR": str(datetime.now(timezone.utc).year),
        },
    )


def build_ai_prompt(industry, domain):
    business_name = business_name_from_domain(domain)
    year = datetime.now(timezone.utc).year
    industry_hint = f" in the {industry.replace('_', ' ')} industry" if industry else ""
    return f"""Generate a complete, single-file HTML webpage for a small business called "{business_name}"{industry_hint}, whose website domain is {domain}.

Requirements:
- Output ONLY raw HTML. No markdown code fences, no commentary before or after the HTML.
- A complete <!doctype html> document with an inline <style> block. No external stylesheets, fonts, images, or scripts.
- Include: a nav bar with the business name, a hero section with a short tagline, a services/features section with 3-4 items, an about section, a contact section using an email address like info@{domain}, and a footer with the copyright year {year} and the business name.
- Choose a distinct, professional color palette and layout appropriate for a business named "{business_name}".
- Make it responsive with at least one @media breakpoint for mobile screens.
- Use the exact business name "{business_name}" and domain "{domain}" in the page (title tag, hero heading, contact email, footer copyright).
"""


AI_TIMEOUT = 120  # seconds — generating a full page can take longer than a plain API call


AI_MAX_TOKENS = 16000


def call_claude(api_key, prompt, model=CLAUDE_MODEL):
    try:
        import anthropic
    except ImportError as e:
        raise AIGenerationError(
            "The 'anthropic' package is required for --ai claude. Install it with: pip install anthropic"
        ) from e
    client = anthropic.Anthropic(api_key=api_key, timeout=AI_TIMEOUT)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=AI_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APITimeoutError as e:
        raise AIGenerationError(f"Claude API timed out after {AI_TIMEOUT}s") from e
    except anthropic.APIError as e:
        raise AIGenerationError(f"Claude API error: {e}") from e

    if response.stop_reason == "max_tokens":
        raise AIGenerationError(
            f"Claude hit the {AI_MAX_TOKENS}-token output limit; the page would be "
            "truncated mid-document. Retry, or raise AI_MAX_TOKENS."
        )
    if response.stop_reason == "refusal":
        raise AIGenerationError("Claude declined to generate this page")

    return "".join(block.text for block in response.content if block.type == "text")


def call_gemini(api_key, prompt, model=GEMINI_MODEL):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        resp = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": AI_MAX_TOKENS},
            },
            timeout=AI_TIMEOUT,
        )
    except requests.Timeout as e:
        raise AIGenerationError(f"Gemini API timed out after {AI_TIMEOUT}s") from e
    data = resp.json()
    if "error" in data:
        raise AIGenerationError(f"Gemini API error: {data['error'].get('message', data['error'])}")
    try:
        candidate = data["candidates"][0]
        text = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise AIGenerationError(f"Unexpected Gemini response shape: {data}") from e

    finish = candidate.get("finishReason")
    if finish and finish != "STOP":
        raise AIGenerationError(
            f"Gemini stopped early (finishReason={finish}); the page would be incomplete."
        )
    return text


def clean_ai_html(text):
    text = text.strip()
    text = re.sub(r"^```(?:html)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    lowered = text.lower()
    if "<html" not in lowered:
        raise AIGenerationError("AI response did not contain a valid HTML document")
    # A response cut off by a token limit still has "<html" but no closing tag;
    # without this check the truncated page deploys as if it were finished.
    if "</html>" not in lowered:
        raise AIGenerationError(
            "AI response is missing a closing </html> tag — the document looks truncated"
        )
    return text


def generate_html_with_ai(provider, api_key, industry, domain, model=None):
    prompt = build_ai_prompt(industry, domain)
    if provider == "claude":
        raw = call_claude(api_key, prompt, model or CLAUDE_MODEL)
    else:
        raw = call_gemini(api_key, prompt, model or GEMINI_MODEL)
    return clean_ai_html(raw)


def render_worker_module(html):
    return f"""export default {{
  async fetch(request) {{
    const html = {json.dumps(html)};
    return new Response(html, {{
      headers: {{ "content-type": "text/html; charset=UTF-8" }}
    }});
  }}
}};
"""


def get_account_id(token, explicit):
    if explicit:
        return explicit
    accounts = cf_request("GET", "/accounts", token)
    if len(accounts) == 1:
        return accounts[0]["id"]
    if len(accounts) == 0:
        raise CloudflareError("This token has no visible accounts. Pass --account-id.")
    names = ", ".join(f"{a['name']} ({a['id']})" for a in accounts)
    raise CloudflareError(
        f"Token can see multiple accounts ({names}). Pass --account-id to pick one."
    )


def zone_candidates(domain):
    """Progressively broader zone names to try for a hostname.

    Cloudflare zones are registrable domains, so 'shop.example.com' has no zone
    of its own — it lives under 'example.com'. Walking up the labels lets a
    subdomain attach to its parent zone instead of falling back to workers.dev.
    """
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def find_active_zone(domain, token, session=None):
    for candidate in zone_candidates(domain):
        zones = cf_request("GET", "/zones", token, session=session, params={"name": candidate})
        if not zones:
            continue
        zone = zones[0]
        if zone["status"] == "active":
            return zone
    return None


COMPATIBILITY_DATE = "2025-01-01"


def upload_worker_script(account_id, script_name, module_code, token, session=None):
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": COMPATIBILITY_DATE,
    }
    files = {
        "metadata": (None, json.dumps(metadata), "application/json"),
        "worker.js": ("worker.js", module_code, "application/javascript+module"),
    }
    cf_request(
        "PUT",
        f"/accounts/{account_id}/workers/scripts/{script_name}",
        token,
        session=session,
        files=files,
    )


def attach_custom_domain(account_id, zone_id, domain, script_name, token, session=None):
    payload = {
        "environment": "production",
        "hostname": domain,
        "service": script_name,
        "zone_id": zone_id,
    }
    cf_request(
        "PUT",
        f"/accounts/{account_id}/workers/domains",
        token,
        session=session,
        json=payload,
    )
    return f"https://{domain}"


def publish_on_workers_dev(account_id, script_name, token, session=None):
    cf_request(
        "POST",
        f"/accounts/{account_id}/workers/scripts/{script_name}/subdomain",
        token,
        session=session,
        json={"enabled": True},
    )
    subdomain = cf_request(
        "GET", f"/accounts/{account_id}/workers/subdomain", token, session=session
    )
    return f"https://{script_name}.{subdomain['subdomain']}.workers.dev"


def list_deployments(account_id, token):
    body = cf_request_body("GET", f"/accounts/{account_id}/workers/scripts", token)
    scripts = body.get("result") or []

    # This endpoint isn't documented as paginated, but if Cloudflare ever
    # reports more scripts than it returned, say so rather than quietly
    # printing a partial list.
    info = body.get("result_info") or {}
    total = info.get("total_count")
    if isinstance(total, int) and total > len(scripts):
        print(
            f"Warning: Cloudflare reports {total} Worker scripts but returned "
            f"{len(scripts)}; this list may be incomplete.",
            file=sys.stderr,
        )

    ours = [s for s in scripts if s["id"].startswith(FLOCKFRONT_PREFIX)]
    if not ours:
        return []

    domains = cf_request("GET", f"/accounts/{account_id}/workers/domains", token)
    hostname_by_service = {
        d["service"]: d["hostname"] for d in domains if d["service"].startswith(FLOCKFRONT_PREFIX)
    }

    workers_subdomain = None
    results = []
    for s in ours:
        name = s["id"]
        hostname = hostname_by_service.get(name)
        if hostname:
            url = f"https://{hostname}"
        else:
            if workers_subdomain is None:
                sub = cf_request("GET", f"/accounts/{account_id}/workers/subdomain", token)
                workers_subdomain = sub["subdomain"]
            url = f"https://{name}.{workers_subdomain}.workers.dev"
        results.append((name, url, s.get("modified_on", "")))
    return results


def delete_deployment(domain, account_id, token):
    domain = validate_domain(domain)
    script_name = script_name_for(domain)

    domains = cf_request("GET", f"/accounts/{account_id}/workers/domains", token)
    match = next((d for d in domains if d["hostname"] == domain), None)
    if match:
        cf_request("DELETE", f"/accounts/{account_id}/workers/domains/{match['id']}", token)

    cf_request("DELETE", f"/accounts/{account_id}/workers/scripts/{script_name}", token)
    return script_name


def load_domains(args):
    domains = list(args.domain or [])
    if args.domains_file:
        path = Path(args.domains_file)
        if not path.is_file():
            raise ValueError(f"--domains-file not found: {args.domains_file}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                domains.append(line)
    if not domains:
        raise ValueError("No domains given. Pass one or more domains, or --domains-file.")
    return dedupe(domains)


def dedupe(domains):
    """Drop repeats (keeping order) so a domain listed twice deploys once."""
    seen = set()
    unique = []
    for domain in domains:
        key = domain.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(domain)
    return unique


def deploy_one(domain, args, account_id, session=None):
    domain = validate_domain(domain)
    script_name = script_name_for(domain)

    if args.ai:
        print(f"Generating {args.ai} website for {domain}...", file=sys.stderr)
        html = generate_html_with_ai(
            args.ai, args.ai_key, args.industry, domain, getattr(args, "ai_model", None)
        )
    else:
        print(f"Rendering {args.industry} template for {domain}...", file=sys.stderr)
        html = render_site_html(args.industry, domain)

    if args.dry_run:
        out_path = Path(f"{script_name}.preview.html").resolve()
        out_path.write_text(html, encoding="utf-8")
        return f"file://{out_path}"

    module_code = render_worker_module(html)

    print(f"Deploying Worker script '{script_name}'...", file=sys.stderr)
    upload_worker_script(account_id, script_name, module_code, args.token, session=session)

    zone = find_active_zone(domain, args.token, session=session)
    if zone:
        print(f"Attaching Worker to {domain} via Workers Custom Domains...", file=sys.stderr)
        return attach_custom_domain(
            account_id, zone["id"], domain, script_name, args.token, session=session
        )

    print(f"No active Cloudflare zone for {domain}; publishing on workers.dev instead...", file=sys.stderr)
    return publish_on_workers_dev(account_id, script_name, args.token, session=session)


def deploy_all(domains, args, account_id):
    """Deploy every domain, returning (domain, url, error) in input order.

    Each worker thread gets its own Session: urllib3's Retry state isn't
    designed to be shared across concurrent requests.
    """
    workers = max(1, min(args.concurrency, len(domains)))

    def run(domain):
        session = build_session() if workers > 1 else SESSION
        try:
            return (domain, deploy_one(domain, args, account_id, session=session), None)
        except (CloudflareError, ValueError, AIGenerationError) as e:
            return (domain, None, f"Error deploying {domain}: {e}")
        except requests.RequestException as e:
            return (domain, None, f"Network error deploying {domain}: {e}")
        finally:
            if workers > 1:
                session.close()

    if workers == 1:
        return [run(domain) for domain in domains]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, domains))


def main():
    parser = argparse.ArgumentParser(
        description="Spin up a themed business website on Cloudflare Workers for one or more domains you own.",
        epilog="Requires CLOUDFLARE_API_TOKEN (or --token). Optional: CLOUDFLARE_ACCOUNT_ID / --account-id.",
    )
    parser.add_argument("domain", nargs="*", help="Domain(s) you own, e.g. example.com")
    parser.add_argument("--domains-file", help="Path to a file with one domain per line (lines starting with # are ignored)")
    parser.add_argument(
        "-i", "--industry",
        choices=sorted(INDUSTRY_TAGLINES),
        default="finance",
        help="Business theme/template to deploy (default: finance). Also used as a hint when --ai is set.",
    )
    parser.add_argument(
        "--ai",
        choices=["claude", "gemini"],
        default=None,
        help="Generate the site with an AI model instead of the static templates",
    )
    parser.add_argument(
        "--ai-key",
        default=None,
        help="API key for the chosen --ai provider (or set ANTHROPIC_API_KEY / GEMINI_API_KEY)",
    )
    parser.add_argument(
        "--ai-model",
        default=None,
        help=f"Override the model for --ai (default: {CLAUDE_MODEL} for claude, {GEMINI_MODEL} for gemini)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        metavar="N",
        help="Deploy up to N domains in parallel (default: 4, use 1 for serial)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the site to a local <script-name>.preview.html file instead of deploying to Cloudflare. With --delete, report what would be removed without deleting it.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every flockFront deployment on the account and exit",
    )
    parser.add_argument(
        "--delete",
        nargs="+",
        metavar="DOMAIN",
        help="Delete the flockFront deployment(s) for the given domain(s) and exit",
    )
    parser.add_argument("--token", default=None,
                         help="Cloudflare API token (or set CLOUDFLARE_API_TOKEN)")
    parser.add_argument("--account-id", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
                         help="Cloudflare account ID (or set CLOUDFLARE_ACCOUNT_ID)")
    args = parser.parse_args()

    print_banner()

    if args.token:
        print(
            "Warning: passing --token on the command line leaves it in your shell history "
            "and process list. Prefer setting CLOUDFLARE_API_TOKEN instead.",
            file=sys.stderr,
        )
        args.token = args.token.strip()
    else:
        args.token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip() or None

    if args.account_id:
        args.account_id = args.account_id.strip()

    if not args.token and not args.dry_run:
        print("Error: no Cloudflare API token. Pass --token or set CLOUDFLARE_API_TOKEN.", file=sys.stderr)
        sys.exit(1)

    if args.ai:
        env_var = "ANTHROPIC_API_KEY" if args.ai == "claude" else "GEMINI_API_KEY"
        if args.ai_key:
            print(
                f"Warning: passing --ai-key on the command line leaves it in your shell history "
                f"and process list. Prefer setting {env_var} instead.",
                file=sys.stderr,
            )
        args.ai_key = (args.ai_key or os.environ.get(env_var, "")).strip()
        if not args.ai_key:
            print(f"Error: no API key for --ai {args.ai}. Pass --ai-key or set {env_var}.", file=sys.stderr)
            sys.exit(1)

    if args.concurrency < 1:
        print("Error: --concurrency must be at least 1.", file=sys.stderr)
        sys.exit(1)

    account_id = None
    # --dry-run never touches Cloudflare, so it needs no account (or token) —
    # including when combined with --delete, which then only reports.
    if args.list or not args.dry_run:
        try:
            print("Resolving Cloudflare account...", file=sys.stderr)
            account_id = get_account_id(args.token, args.account_id)
        except CloudflareError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except requests.RequestException as e:
            print(f"Network error talking to Cloudflare API: {e}", file=sys.stderr)
            sys.exit(1)

    if args.list:
        try:
            deployments = list_deployments(account_id, args.token)
        except CloudflareError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except requests.RequestException as e:
            print(f"Network error talking to Cloudflare API: {e}", file=sys.stderr)
            sys.exit(1)
        if not deployments:
            print("No flockFront deployments found on this account.")
        else:
            print(f"{len(deployments)} flockFront deployment(s):")
            for name, url, modified in deployments:
                when = modified[:19].replace("T", " ") if modified else "unknown"
                print(f"  {url}  ({name}, last modified {when})")
        return

    if args.delete:
        failures = 0
        for domain in dedupe(args.delete):
            try:
                if args.dry_run:
                    script_name = script_name_for(validate_domain(domain))
                    print(f"Would delete {domain} (worker '{script_name}')")
                    continue
                script_name = delete_deployment(domain, account_id, args.token)
                print(f"Deleted {domain} (worker '{script_name}')")
            except (CloudflareError, ValueError) as e:
                print(f"Error deleting {domain}: {e}", file=sys.stderr)
                failures += 1
            except requests.RequestException as e:
                print(f"Network error deleting {domain}: {e}", file=sys.stderr)
                failures += 1
        sys.exit(1 if failures else 0)

    try:
        domains = load_domains(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    failures = 0
    for _domain, url, error in deploy_all(domains, args, account_id):
        if error:
            print(error, file=sys.stderr)
            failures += 1
        else:
            print(url)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
