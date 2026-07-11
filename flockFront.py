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

Output:
  Prints the live URL for each domain once its site is deployed.
"""

import argparse
import json
import os
import re
import string
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import requests

API_BASE = "https://api.cloudflare.com/client/v4"
TEMPLATES_DIR = Path(__file__).parent / "templates"

INDUSTRY_TAGLINES = {
    "finance": "Your trusted partner in financial growth.",
    "healthcare": "Compassionate care, close to home.",
    "education": "Empowering minds, building futures.",
    "legal": "Experienced counsel, straightforward advice.",
    "real_estate": "Local expertise for every move you make.",
    "restaurant": "Great food, made for gathering.",
    "fitness": "Real coaching. Real community. Real results.",
}

CLAUDE_MODEL = "claude-opus-4-8"
GEMINI_MODEL = "gemini-3.5-flash"


class CloudflareError(Exception):
    pass


class AIGenerationError(Exception):
    pass


def cf_request(method, path, token, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, f"{API_BASE}{path}", headers=headers, **kwargs)
    try:
        body = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise CloudflareError(f"Unexpected non-JSON response from {path}")

    if not body.get("success", False):
        errors = body.get("errors", [])
        msg = "; ".join(e.get("message", str(e)) for e in errors) or resp.text
        raise CloudflareError(f"Cloudflare API error ({method} {path}): {msg}")

    return body.get("result")


def validate_domain(domain):
    pattern = r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$"
    if not re.match(pattern, domain):
        raise ValueError(f"'{domain}' doesn't look like a valid domain name")
    return domain.lower()


def slugify(domain):
    return re.sub(r"[^a-z0-9-]", "-", domain.lower())


def business_name_from_domain(domain):
    base = domain.split(".")[0]
    words = re.split(r"[-_]+", base)
    return " ".join(w.capitalize() for w in words if w)


def render_site_html(industry, domain):
    template_path = TEMPLATES_DIR / f"{industry}.html"
    tpl = string.Template(template_path.read_text(encoding="utf-8"))
    return tpl.substitute(
        BUSINESS_NAME=escape(business_name_from_domain(domain)),
        DOMAIN=escape(domain),
        TAGLINE=escape(INDUSTRY_TAGLINES[industry]),
        YEAR=str(datetime.now(timezone.utc).year),
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


def call_claude(api_key, prompt):
    try:
        import anthropic
    except ImportError:
        raise AIGenerationError(
            "The 'anthropic' package is required for --ai claude. Install it with: pip install anthropic"
        )
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        raise AIGenerationError(f"Claude API error: {e}")
    return "".join(block.text for block in response.content if block.type == "text")


def call_gemini(api_key, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    resp = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
    )
    data = resp.json()
    if "error" in data:
        raise AIGenerationError(f"Gemini API error: {data['error'].get('message', data['error'])}")
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AIGenerationError(f"Unexpected Gemini response shape: {data}")


def clean_ai_html(text):
    text = text.strip()
    text = re.sub(r"^```(?:html)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    if "<html" not in text.lower():
        raise AIGenerationError("AI response did not contain a valid HTML document")
    return text


def generate_html_with_ai(provider, api_key, industry, domain):
    prompt = build_ai_prompt(industry, domain)
    if provider == "claude":
        raw = call_claude(api_key, prompt)
    else:
        raw = call_gemini(api_key, prompt)
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


def find_active_zone(domain, token):
    zones = cf_request("GET", "/zones", token, params={"name": domain})
    if not zones:
        return None
    zone = zones[0]
    return zone if zone["status"] == "active" else None


def upload_worker_script(account_id, script_name, module_code, token):
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": "2024-01-01",
    }
    files = {
        "metadata": (None, json.dumps(metadata), "application/json"),
        "worker.js": ("worker.js", module_code, "application/javascript+module"),
    }
    cf_request(
        "PUT",
        f"/accounts/{account_id}/workers/scripts/{script_name}",
        token,
        files=files,
    )


def attach_custom_domain(account_id, zone_id, domain, script_name, token):
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
        json=payload,
    )
    return f"https://{domain}"


def publish_on_workers_dev(account_id, script_name, token):
    cf_request(
        "POST",
        f"/accounts/{account_id}/workers/scripts/{script_name}/subdomain",
        token,
        json={"enabled": True},
    )
    subdomain = cf_request("GET", f"/accounts/{account_id}/workers/subdomain", token)
    return f"https://{script_name}.{subdomain['subdomain']}.workers.dev"


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
    return domains


def deploy_one(domain, args, account_id):
    domain = validate_domain(domain)
    script_name = f"flockfront-{slugify(domain)}"[:63]

    if args.ai:
        print(f"Generating {args.ai} website for {domain}...", file=sys.stderr)
        html = generate_html_with_ai(args.ai, args.ai_key, args.industry, domain)
    else:
        print(f"Rendering {args.industry} template for {domain}...", file=sys.stderr)
        html = render_site_html(args.industry, domain)
    module_code = render_worker_module(html)

    print(f"Deploying Worker script '{script_name}'...", file=sys.stderr)
    upload_worker_script(account_id, script_name, module_code, args.token)

    zone = find_active_zone(domain, args.token)
    if zone:
        print(f"Attaching Worker to {domain} via Workers Custom Domains...", file=sys.stderr)
        return attach_custom_domain(account_id, zone["id"], domain, script_name, args.token)

    print(f"No active Cloudflare zone for {domain}; publishing on workers.dev instead...", file=sys.stderr)
    return publish_on_workers_dev(account_id, script_name, args.token)


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
    parser.add_argument("--token", default=os.environ.get("CLOUDFLARE_API_TOKEN"),
                         help="Cloudflare API token (or set CLOUDFLARE_API_TOKEN)")
    parser.add_argument("--account-id", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
                         help="Cloudflare account ID (or set CLOUDFLARE_ACCOUNT_ID)")
    args = parser.parse_args()

    if args.token:
        args.token = args.token.strip()
    if args.account_id:
        args.account_id = args.account_id.strip()

    if not args.token:
        print("Error: no Cloudflare API token. Pass --token or set CLOUDFLARE_API_TOKEN.", file=sys.stderr)
        sys.exit(1)

    if args.ai:
        env_var = "ANTHROPIC_API_KEY" if args.ai == "claude" else "GEMINI_API_KEY"
        args.ai_key = (args.ai_key or os.environ.get(env_var, "")).strip()
        if not args.ai_key:
            print(f"Error: no API key for --ai {args.ai}. Pass --ai-key or set {env_var}.", file=sys.stderr)
            sys.exit(1)

    try:
        domains = load_domains(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        print("Resolving Cloudflare account...", file=sys.stderr)
        account_id = get_account_id(args.token, args.account_id)
    except CloudflareError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"Network error talking to Cloudflare API: {e}", file=sys.stderr)
        sys.exit(1)

    failures = 0
    for domain in domains:
        try:
            url = deploy_one(domain, args, account_id)
            print(url)
        except (CloudflareError, ValueError, AIGenerationError) as e:
            print(f"Error deploying {domain}: {e}", file=sys.stderr)
            failures += 1
        except requests.RequestException as e:
            print(f"Network error deploying {domain}: {e}", file=sys.stderr)
            failures += 1

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
