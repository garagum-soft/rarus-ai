# Accounts mode: one Raven server for several people

Accounts mode turns one machine into a Raven server several people sign in to.
Each person gets an engine of their own — their own keys and subscriptions,
conversations, agents, employees and messengers — and pays for model use in
credits priced by the administrator. It is off unless you start it; a Raven
started any other way is unchanged.

## Start it

```bash
raven accounts serve --host 127.0.0.1 --port 8800
```

- `--host 0.0.0.0` listens on the local network; for a domain, keep
  `127.0.0.1` and put an HTTPS reverse proxy in front (below).
- Every account's engine is started when the gateway starts
  (`--no-autostart` starts each on its owner's first visit instead), adopted
  when it is already running, and restarted if it exits.
- Engines keep running when the gateway stops, so schedules and messengers go
  on; `raven accounts stop-engines` stops them all (the superadmin's too,
  which is the server's own Raven).

### The first account

The first account is the **superadmin**: it can do everything, and its Raven is
the one already on this machine (the server's own home). It can only be made
from the server itself:

- open `http://localhost:8800` **on the server** — the page offers to create
  it; a request that came through a proxy or from another machine is refused;
- or run `raven accounts init --username <name>` in the server's terminal.

## A domain and HTTPS

Google Sign-In needs an `https://` address (or `http://localhost`). Point the
domain at the server and put a reverse proxy in front of the gateway, e.g.
Caddy, which fetches the certificate itself:

```
raven.example.com {
    reverse_proxy 127.0.0.1:8800
}
```

The proxy must pass WebSocket upgrades through (Caddy and nginx with the usual
`Upgrade`/`Connection` headers do) and keep the `Host` header. Then set the
public address in the admin page (Settings → Public address), or:

```bash
raven accounts set public_url https://raven.example.com
```

## Google Sign-In

1. In the Google Cloud console, create an OAuth client of type *Web application*.
2. Add the authorized redirect URI shown in the admin page:
   `https://raven.example.com/_hub/google/callback`.
3. Enter the client ID and client secret in the admin page (Settings), or
   `raven accounts set google_client_id <id>` and
   `raven accounts set google_client_secret` (asked for hidden).

The sign-in page then offers "Sign in with Google". A Google account signs in
to the account it was linked to; a new one creates an account when
registration is open. A person who registered with a password links Google from
their account page. The client secret is never shown again.

## Credits and prices

The administrator sets, per model, three prices in US dollars per million
tokens — input, output, and cache hit — and how many credits a dollar is
(default 100). A call is charged its price in credits times a share:

| The call ran on | Share |
|---|---|
| access the server lends the account (the admin's keys or subscriptions) | 1 (the full price) |
| the account's own key or subscription | 0.1 |

- `*` prices every model the table does not name; `cli:*` prices external CLI
  agents (Claude Code, Codex CLI…), whose usage is the tokens they reported or
  an estimate from the text that went in and came out (marked *estimated*).
- A fresh server starts with placeholder `*` and `cli:*` prices so nothing is
  free by accident. Models used without a price of their own are listed in the
  admin page.
- Usage is charged every half minute from each engine's usage files. At zero
  credits (or over the 50 GB disk allowance — administrators have none) an
  account starts no new work: no new conversation turn, scheduled job or
  background agent. Work that was already running finishes, so a balance can
  go below zero by that much. A top-up lifts the block at once.

```bash
raven accounts price anthropic/claude-sonnet-4-5 --input 3 --output 15 --cache-hit 0.3
raven accounts topup alice 500 --note "October"
```

## Lending the server's access

In the admin page, an account's *Lent access* lists the providers configured in
the server's own config and the server's subscriptions (`claude_subscription`,
`openai_codex`). A lent provider's key is copied into the account's engine; a
lent subscription points the engine at the server's login. Everything lent is
charged at the full price. Withdrawing a provider removes it from the engine
only while it is still the server's copy — a key the person entered under the
same name stays.

## Roles and permissions

- **superadmin** — everything; the only one who makes or unmakes admins.
- **admin** — the permissions granted: `points.topup`, `users.manage`
  (create, disable, lend access, restart engines), `prices.edit`,
  `settings.edit`, `audit.read`.
- **user** — their own account.

Every administrative action is in the audit log (admin page → Audit;
`raven accounts audit`). Passwords are stored as scrypt hashes; sign-in sessions
are HttpOnly, SameSite=Strict cookies; failed sign-ins are throttled.

## Each person's own team

Every engine has the settings Team section (and `raven agents hire`): hire an
employee with a name, a role, a model on that account's keys or a key of its
own, and a system prompt. Employees live in that account's home, so every
person's team is their own.

## Know before you open it to people

- **The separation is Raven's, not the operating system's.** Every engine runs
  as the server's own OS user. An agent that may run shell commands without a
  sandbox can read other accounts' files, keys included. Turn on a sandbox in
  each engine (`tools.sandbox.backend`) or give accounts only to people you
  trust.
- The browser tool's profile (`~/.raven/browser-profile`) is shared by the
  engines; two people driving the browser tool at once collide.
- MCP servers that sign in with OAuth redirect to the server's own loopback,
  so a person on another machine cannot complete that sign-in.
- A messenger that needs a fixed local port (the WhatsApp bridge, 3001) can be
  connected by one account at a time.
