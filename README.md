# appadino-discovery
Nonprofit prospect discovery pipeline

What this is

DiscoveryAI
A multi-tenant pipeline that ingests the national IRS nonprofit universe, applies a client's ICP as configuration, scores survivors with the Claude API, suppresses known contacts, optionally enriches approved prospects with verified contact data (candidate stage, pending validation — see §5.5), and produces a human-reviewable prospect list with citations. A web dashboard for review ships after the pipeline; an automated intake flow after that. Outreach is always human-gated — this system never sends anything autonomously.
