# Access control proposal — v1 item 5 (one customer)

Status: **proposal only, awaiting Duan's decision.** Nothing here is built.
Written 2026-11-12.

Goal (from STATUS.md item 5): someone who is not logged in can't read any
prospects, and the API works out which customer's data to show (`client_id`)
from who is logged in. The browser never gets to choose it.

---

## 1. How things are deployed and protected today

These findings come from the repo only. I did not look in the Azure portal, so
if something was created there by hand, it isn't covered here.

| Question | Answer |
|---|---|
| Is the dashboard live on Azure Static Web Apps? | **No.** No Static Web App or Function App is defined anywhere in `infra/` (it has only Postgres, Key Vault, storage, the container registry, the Container Apps pipeline job, App Insights and alerts). STATUS.md and `dashboard/functions_api/README.md` both say it runs only locally. The two GitHub workflows deploy only the pipeline infra and run pipeline tests. Nothing deploys the dashboard. |
| How does it run? | On your PC: `func start` serves the API on port 7071 and the Vite dev server serves the React app, sending `/api/*` calls on to port 7071. |
| Is the API reachable from the internet? | **No.** It exists only while `func start` runs on your machine. (While it runs, other devices on the same home network may be able to reach port 7071 if Windows Firewall allows it. That is a small local risk, not an internet one.) |
| What protects it? | **Nothing in the app.** `function_app.py:44` sets every route to `AuthLevel.ANONYMOUS`, meaning no login and no key. The example local settings also set CORS (the browser rule for which websites may call the API) to `*`, meaning any website. |
| Could an anonymous caller read prospects? | **Yes, if it were ever put online as it is today.** Anyone could call `GET /api/prospects?client_id=2` or `/api/export.csv?client_id=2` and get everything. They could also list every customer (`/api/clients`), change statuses and notes, delete suppression entries, and start **paid** FullEnrich lookups (`POST /api/prospects/{id}/enrich`). Right now the only thing preventing this is that the API isn't online. |

Two more gaps any option has to close, whatever login method we pick:

1. **`client_id` comes from the request.** Every read endpoint takes it from
   the URL (`?client_id=`), and `POST /api/suppression` takes it from the body.
   The dashboard's client dropdown (`App.tsx`, `/api/clients`) is a dev-only
   tool for switching between customers.
2. **Routes that act on one item by its id don't check who owns it.**
   `PATCH /api/prospects/{id}`, `.../enrich`, `.../enrich-status`,
   `.../suppression-review` and `DELETE /api/suppression/{id}` find the row
   by id alone. Even with login in place, a customer could type in another
   customer's prospect id. (Security people call this an "IDOR", meaning
   guessing or changing an id to reach someone else's record.) Each of these
   routes needs a "does this row belong to the logged-in customer?" check.
3. **`updated_by` is whatever name the browser sends** (typed by the
   reviewer and kept in the browser's localStorage). After this change it
   should come from the login.

Not part of this item but worth knowing: Postgres is protected by a password
plus two firewall rules: `AllowAzureServices`, which lets in any service
running in Azure, and `AllowDevMachine`. STATUS.md item 7 already tracks
cleaning this up.

---

## 2. What every option shares

- **A table that links people to customers.** A new table, `client_users`,
  holds one row per allowed person: login provider, email, `client_id`, and
  an `active` flag. On every request the API finds the logged-in person in
  this table to get their `client_id`. Not found or inactive means **403
  (refused)**. The `client_id` query and body parameters are removed. This
  table is the single source of truth for "who sees what". Turning off a row
  cuts that person off on their next click, whatever their browser still
  remembers.
- **Ownership checks** on the five routes in gap 2. If the prospect or
  suppression row belongs to another customer, the API answers **404 (not
  found)**, which doesn't even confirm that the row exists.
- **`updated_by` comes from the logged-in email.**
- **Customer-facing changes to the dashboard.** The client dropdown is
  removed for customers, and so is the "your name" box.
- **You keep using your local `func start` setup for viewing several
  customers.** The deployed site is customer-only. This avoids building an
  "operator can view any customer" mode, which would put back the exact
  "caller picks `client_id`" risk we are removing. It can be added later if
  you need it.
- **A first real deployment of the dashboard and API.** This work is needed
  whichever option you pick, because nothing is deployed yet. It includes
  the step already flagged in the Functions README: the API imports the
  `discovery` package from `../../pipeline`, and that folder won't exist in a
  deployment upload. The fix is to build `discovery` as a wheel (Python's
  install-ready package file) at deploy time and include it in the upload.
  It is estimated separately below as **about 6 hours**, shared by all
  options.

---

## 3. The options

### Option A — Azure Static Web Apps with built-in login and an invite-only role (recommended)

**What it is.** Static Web Apps (SWA) is Azure's hosting for a website plus a
small API behind it on the same web address. It has login built in: you turn
it on with a config file (`staticwebapp.config.json`) and don't write any
login code. The API code runs as SWA's "managed functions", which are the
same Azure Functions code, hosted by SWA and reachable only through the
site's own `/api` address.

**How a customer logs in.** They open the site. If they aren't logged in, the
site sends them to Microsoft's normal sign-in page. They sign in with their
Microsoft work or personal account, and they land back on the dashboard
showing only their prospects. The config file blocks GitHub login so that
only Microsoft sign-in is offered.

**How `client_id` is derived.** After sign-in, SWA attaches a header called
`x-ms-client-principal` to every API call. It is a small note saying who the
user is (email, provider, roles). **SWA writes this header itself and
overwrites anything a browser tries to send in its place**, so a caller can't
fake it. The API reads the email from it, looks it up in `client_users`, and
uses that row's `client_id`. If the header is missing, the answer is 401
(not logged in). If the email isn't in the table, the answer is 403.

Two locks, so one mistake doesn't expose data:

1. **SWA lock:** every page and `/api/*` route requires a custom role, for
   example `customer`. In SWA you can give someone a custom role only by
   invitation, so a random person with a Microsoft account gets turned away
   before any code runs.
2. **API lock:** the `client_users` lookup above.

**How you add a customer.**
1. Azure portal → the Static Web App → *Role management* → *Invite*. Enter
   their email, pick Microsoft as the provider, and give them the role
   `customer`. Copy the invite link and email it to them. The link expires
   after at most 7 days.
2. Insert one row into `client_users` (email, `client_id`). This goes into
   the onboarding runbook next to the existing "insert an `icp_configs` row"
   step.

**How you remove a customer.** Set their `client_users` row to
`active = false`, which cuts them off on their next click. Then remove their
role in *Role management* so they can't get past the front door either.

**Effort:** about **9 hours** of auth work, plus about 6 hours for the first
deployment = **about 15 hours** (range 12–18).
- SWA resource in Bicep (Azure's infrastructure-as-code files), the deploy
  workflow, and app settings: about 2 h, beyond the shared 6 h.
- `staticwebapp.config.json` (route locks, sending logged-out visitors to
  login, blocking GitHub): about 1 h.
- API: read the header, `client_users` table plus migration, remove
  `client_id` params, ownership checks on five routes, `updated_by` from the
  login: about 4 h.
- Dashboard: remove the dropdown and name box, handle 401/403: about 1 h.
- Tests, plus inviting a test account and doing an end-to-end check with a
  real login: about 1–2 h.

**Monthly cost: $0** on SWA's Free plan. Free covers custom domains, built-in
login with Microsoft or GitHub, invitations, and the managed API. It becomes
**about $9/month** (Standard plan) only if one of the "could go wrong" items
below forces it.

**What could go wrong.**
- **The customer doesn't use Microsoft accounts.** If Lauren's organization
  uses Google Workspace, Free-plan login won't fit. Any email can be turned
  into a personal Microsoft account, but that's friction for her. Adding
  Google means Standard plan ($9/mo) and about 2 more hours to register a
  custom login provider. **Ask Lauren first.**
- **Using SWA's built-in `authenticated` role by mistake.** That role means
  "anyone with any Microsoft account". The lock must be the invite-only
  custom role. The API lock is the safety net if this config slips.
- **Managed-function limits.** SWA's managed API supports only HTTP triggers
  (fine here, since all 11 routes are HTTP) and has a short per-request time
  limit, about 45 seconds. The FullEnrich flow already submits and then
  polls `enrich-status`, so it should fit, but it needs checking against
  real latency. If anything doesn't fit, the fallback is Standard ($9/mo)
  with a separate Function App linked behind the site.
- **Secrets.** On Free, `DATABASE_URL` and the FullEnrich key are stored as
  SWA app settings. They are encrypted by Azure and not visible to users,
  but they don't come from Key Vault. Standard can use Key Vault through a
  managed identity.
- **Wheel bundling** (the shared deploy step) is the most likely place to
  lose time. It is a packaging problem, not a security one.
- **Someone keeps a valid login cookie** for a while after you remove their
  role. The `client_users` switch-off covers this.

### Option B — A separate Azure Function App with App Service built-in login ("Easy Auth")

**What it is.** The API is deployed as its own Function App, and Azure's
built-in login for App Service ("Easy Auth") is switched on in front of it.
Login uses Microsoft Entra ID (Microsoft's account and sign-in service). You
create an "app registration", which is the record telling Microsoft that this
app is allowed to sign people in. Customers are added to your Entra directory
as **guest users**, and the app is set to "assignment required", so only
people you've explicitly assigned can sign in. The React site still needs
hosting somewhere: blob storage, or SWA anyway.

**How a customer logs in.** You add them as a guest, which sends them a
Microsoft invite email. They accept it, then sign in with their own work
account. If the website and the API end up on different web addresses, the
dashboard must also be changed to sign in through Microsoft's browser
library (MSAL) and attach a sign-in token to each API call. That is extra
code.

**How `client_id` is derived.** Easy Auth adds the same kind of trusted
`X-MS-CLIENT-PRINCIPAL` header (it removes any copy a caller sends). The
same `client_users` lookup applies.

**Add a customer:** Entra → invite a guest → assign them to the app → insert
a `client_users` row. **Remove:** turn off the row, then remove the
assignment or delete the guest.

**Effort:** about **14 hours** of auth work plus about 6 hours for the first
deployment = **about 20 hours** (range 16–26). The extra time over A goes to
the Function App, its storage, app registration, guest setup, and
(probably) MSAL sign-in code in the dashboard because the two parts are on
different addresses.

**Monthly cost: about $1–3.** Flex Consumption Function App at this traffic
is about $0, its storage account under $1, Entra guest sign-ins free at this
size, blob-hosted website pennies. Or $0–9 if the site is hosted on SWA.

**What could go wrong.** More pieces and more settings to get right: app
registration, redirect addresses, cookie or token handling across two
addresses, CORS. More of the security depends on portal settings rather
than code you can read in the repo. Guest invites can land in spam or be
blocked by the customer's own IT. It doesn't do anything Option A can't
for one customer. Its advantage is stronger company-grade controls (for
example requiring the customer's own multi-factor login), which matter more
at many customers.

### Option C — API key behind the Functions API

**What it is.** Each customer gets a long random secret string (an "API
key"). The API stores only a scrambled fingerprint of it (a "hash", so a
database leak doesn't leak the key) next to their `client_id`. Every call
must send the key in a header.

**How a customer logs in.** There is no real login. The dashboard shows a
"paste your access key" box and remembers the key in the browser.

**How `client_id` is derived.** The API hashes the key it received, finds the
matching row, and uses its `client_id`. It is still never taken from the
URL.

**Add a customer:** generate a key, store its hash with their `client_id`,
and send them the key by a safe channel. **Remove:** delete or deactivate
the row.

**Effort:** about **5 hours** of auth work plus about 6 hours for the first
deployment = **about 11 hours.**

**Monthly cost: $0** (hosted on SWA Free or a Function App).

**What could go wrong.** A key is a password the whole team can share,
forward, or paste into a chat, and you'd never know. There is no per-person
identity, so `updated_by` goes back to self-reported names. Anyone with
access to a browser where the key was saved has full access. Rotating
(replacing) a key means sending a new secret. You'd also be writing and
maintaining your own login security code. This is the right tool when one
**program** calls another, not when **people** log into a website.

---

## 4. Side by side

| | A. SWA built-in login | B. Function App + Easy Auth | C. API key |
|---|---|---|---|
| Customer experience | Normal Microsoft sign-in | Guest invite, then Microsoft sign-in | Paste a secret key |
| Knows *which person* acted | Yes | Yes | No |
| Can a caller fake their `client_id`? | No (header set by Azure + DB lookup) | No (same) | No, but a leaked key is a full login |
| Add / remove a customer | Portal invite + 1 DB row / flip row + remove role | Guest invite + assignment + 1 DB row | Make key + 1 DB row / delete row |
| Effort (incl. ~6 h first deploy) | **~15 h** | ~20 h | ~11 h |
| Monthly cost | **$0** (or $9 if Google login needed) | ~$1–3 | $0 |
| Moving parts | Few | Most | Few, but you own the security code |

---

## 5. Recommendation: Option A

In plain terms: **Option A gives a real "sign in with your account" front
door, run by Microsoft, for $0 a month and about two working days, and the
API double-checks every request against a list you control.**

Why A over the others:

- **Compared with C (API key):** it costs about 4 more hours, and in return
  you know exactly which person did what, there's no shared secret to leak,
  and you don't maintain your own password-like system.
- **Compared with B (Easy Auth):** same security result for one customer,
  with fewer pieces, no guest accounts in your directory, the website and
  API on one address (so no cross-site sign-in plumbing), and lower cost.
  B's company-grade extras aren't needed for one customer. If you later have
  many customers whose IT departments want those controls, moving from A to
  B is a contained change, because the `client_users` table and ownership
  checks carry over unchanged.
- **The `client_users` table is the real control,** not the login screen.
  Even if the SWA route config were wrong, the API would still refuse
  anyone who isn't in the table, and it never accepts a `client_id` from the
  browser.

**One question before building:** does Lauren sign in with a Microsoft
account (Outlook/Office 365) or Google? Microsoft means Free plan, about 15
hours. Google means Standard plan ($9/mo), about 17 hours.

**Proposed done-check for item 5** (to run after you approve and it's
built):
- Logged out: the page and every `/api/*` call return 401 or redirect to
  login, and no prospect data comes back.
- Logged in as a Microsoft account that wasn't invited: refused.
- Logged in as the invited customer: sees only their `client_id`. Adding
  `?client_id=<other>` changes nothing. Patching, enriching or deleting
  another customer's row id returns 404.
- Row switched off in `client_users`: the next request is refused.
