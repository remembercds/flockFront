# flockFront

<img width="608" height="505" alt="image" src="https://github.com/user-attachments/assets/1f59c7da-ed96-4621-a866-4085a1e95214" />

Spin up a themed business website on Cloudflare Workers, live under a domain
you own, in one command:

```
python flockFront.py example.com --industry finance
```

Pick an industry template and the tool renders it — business name, page
titles, and contact addresses are all generated from the domain you pass in
— then deploys it as a Cloudflare Worker and prints the live URL.

Available industries (`-i`/`--industry`, default `finance`):

| Industry | Theme |
|---|---|
| `finance` | Navy & gold, corporate |
| `healthcare` | Teal & white, clinical |
| `education` | Blue & orange, friendly |
| `legal` | Charcoal & burgundy, serif |
| `real_estate` | Cream & green, warm |
| `restaurant` | Near-black & gold, serif |
| `fitness` | Black & volt green, bold |

## Prerequisites

### 1. A Cloudflare account with the domain added as a zone

flockFront attaches the site to your domain using **Workers Custom Domains**,
which requires the domain to already be an **active zone** in your Cloudflare
account (i.e. its nameservers point at Cloudflare).

If you haven't done this yet:

1. Log in to the [Cloudflare dashboard](https://dash.cloudflare.com/).
2. Click **Add a domain** and enter your domain (e.g. `example.com`).
3. Choose a plan (the Free plan is enough for this).
4. Cloudflare will scan your existing DNS records and show you two
   nameservers (e.g. `ns1.cloudflare.com`, `ns2.cloudflare.com`).
5. Go to your domain registrar (GoDaddy, Namecheap, Google Domains, etc.) and
   replace the existing nameservers with the two Cloudflare gave you.
6. Wait for propagation. Cloudflare emails you once the zone becomes
   **Active** — this can take anywhere from a few minutes to 24 hours.

You can check zone status any time in the dashboard, or with:

```
curl -s "https://api.cloudflare.com/client/v4/zones?name=example.com" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | grep -o '"status":"[a-z]*"'
```

**If the zone isn't active yet**, flockFront still works — it automatically
falls back to publishing the site on a free `*.workers.dev` subdomain instead
of your domain, so you always get a live URL back. Re-run the tool once the
zone goes active to move the site onto your actual domain.

### 2. A Cloudflare API token with the right permissions

Don't use your Global API Key — create a scoped token instead:

1. Go to [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens).
2. Click **Create Token** → **Create Custom Token**.
3. Add these permissions (note two are under the **Zone** category and one
   is under **Account** — the dashboard has two separate permission
   dropdowns for this reason):
   - **Zone → Zone → Read**
   - **Zone → Workers Routes → Edit** (this covers Workers Custom Domains;
     it's grouped under Zone, not Account)
   - **Account → Workers Scripts → Edit**
4. Under **Zone Resources**, scope it to the specific zone(s) you'll deploy
   to (or "All zones" if you'll use it for multiple domains).
5. Under **Account Resources**, scope it to your account.
6. Create the token and copy it — Cloudflare only shows it once.

Set it in your shell:

```
export CLOUDFLARE_API_TOKEN=your-token-here
```

(On Windows PowerShell: `$env:CLOUDFLARE_API_TOKEN = "your-token-here"`)

Prefer the environment variable over `--token`/`--ai-key` on the command line — either still works, but flockFront prints a warning when you pass a secret inline, since it then sits in your shell history and is visible to anyone who can list processes on the machine.

### 3. Your Cloudflare account ID (only if you have multiple accounts)

If your API token can only see one Cloudflare account, flockFront finds it
automatically — you don't need to do anything.

If you belong to multiple Cloudflare accounts, find the right account ID on
the dashboard's right sidebar (any domain's **Overview** page), then set:

```
export CLOUDFLARE_ACCOUNT_ID=your-account-id-here
```

or pass it per-run with `--account-id`.

### 4. Python dependencies

```
pip install -r requirements.txt
```

This installs `requests`, the only dependency.

## Usage

```
python flockFront.py <domain>... [-i INDUSTRY] [--ai {claude,gemini}] [--ai-key KEY]
                                  [--domains-file FILE] [--token TOKEN] [--account-id ACCOUNT_ID]
```

Examples:

```
python flockFront.py sunrise-wealth.com --industry finance
python flockFront.py cedar-clinic.org -i healthcare
python flockFront.py bright-path.edu -i education
```

If `--industry`/`-i` is omitted, it defaults to `finance`.

On success, the tool prints the live URL:

```
https://sunrise-wealth.com
```

(or `https://flockfront-sunrise-wealth-com.<your-subdomain>.workers.dev` if
the domain isn't an active Cloudflare zone yet).

### Deploying multiple domains at once

Pass several domains directly, a file with one domain per line, or both:

```
python flockFront.py a.com b.com c.com -i fitness
python flockFront.py --domains-file domains.txt -i legal
```

`domains.txt` format (lines starting with `#` are ignored):

```
# clients
sunrise-wealth.com
cedar-clinic.org
```

The Cloudflare account is resolved once; each domain is deployed independently. If one domain fails (e.g. token lacks access to that zone), flockFront prints the error and continues with the rest — it exits with a non-zero status only if at least one domain failed.

### Generating the site with AI instead of the templates

Pass `--ai claude` or `--ai gemini` to have the model design the page from scratch — based on the business name derived from the domain and the `--industry` value as a style hint — instead of using the built-in templates:

```
export ANTHROPIC_API_KEY=sk-ant-...
python flockFront.py sunrise-wealth.com --ai claude

export GEMINI_API_KEY=...
python flockFront.py sunrise-wealth.com --ai gemini -i fitness
```

Or pass the key directly with `--ai-key` instead of an environment variable.

- **Claude** uses the official `anthropic` Python SDK — install it with `pip install anthropic` (not included in `requirements.txt` since it's only needed for this flag).
- **Gemini** talks to the API directly over HTTP — no extra install needed.

If the model's response isn't a complete HTML document (e.g. it refused, or returned something unexpected), flockFront reports an error for that domain instead of deploying broken output.

### Previewing without deploying

Pass `--dry-run` to render the site to a local `<script-name>.preview.html` file instead of touching Cloudflare at all — no token required:

```
python flockFront.py sunrise-wealth.com --dry-run -i legal
python flockFront.py sunrise-wealth.com --ai claude --ai-key sk-ant-... --dry-run
```

Useful for checking AI-generated output (which is non-deterministic) or iterating on a template before spending an actual deploy. `*.preview.html` is gitignored.

### Listing and deleting deployments

```
python flockFront.py --list
python flockFront.py --delete sunrise-wealth.com other-domain.com
```

`--list` enumerates every Worker script whose name starts with `flockfront-` on the account, resolving each to its live URL (custom domain if attached, otherwise its `*.workers.dev` address) and last-modified time.

`--delete` detaches the Workers Custom Domain (if any) and deletes the underlying Worker script for each domain given — this is a real teardown, not reversible. Both flags exit immediately after running and ignore `domain`/`--industry`/`--ai`.

## Troubleshooting

- **"No Cloudflare zone found" behavior / site ends up on workers.dev
  instead of my domain** — the domain isn't an active zone yet. Re-check
  step 1 above and re-run once the zone status is `active`.
- **"Cloudflare API error ... authentication error"** — your token is
  missing, expired, or scoped to the wrong account/zone. Re-check step 2.
- **"Token can see multiple accounts"** — set `CLOUDFLARE_ACCOUNT_ID` or pass
  `--account-id` as described in step 3.
- **Re-running the tool** is safe — it overwrites the same Worker script
  each time (named `flockfront-<domain-with-dashes>`), so running it again
  with a different `--industry` just redeploys the new template.
