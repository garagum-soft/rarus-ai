# Raven for a household

One Raven has no accounts. Everyone signed in to it shares one list of
conversations, one memory, one set of files and the same model keys, and any of
them can change its settings. So a household runs **one Raven per person**: each
member gets their own container, port, data directory and sign-in link, all
from one image built once.

## On the server

Docker with the Compose plugin, and bash. Every member's Raven is a whole
engine, so plan on roughly 1 GB of memory per member while it works.

```bash
cd docker/family
./family.sh build                 # once, and again after updating the checkout
./family.sh add dad
./family.sh add mom
./family.sh add kid --size 10G    # optional disk limit, needs sudo (below)
./family.sh up                    # start everyone
./family.sh signin mom            # a one-time link for mom's own device
```

`signin` prints a link like `http://192.168.1.10:18802/auth#...`. Open it once
on that person's phone or laptop; the browser stays signed in for 30 days, and
`signin` again gives a new link. Links name the server's first IP address; set
`FAMILY_HOST` to use a name such as `raven.home` instead.

`./family.sh list` shows each member's port, whether their Raven is running,
how much disk it uses and its limit.

## What each member has

- **Conversations, memory and files** of their own, under `data/<member>/`;
  nobody else's Raven can read them.
- **Model keys** of their own: they add a provider in the page's
  Settings > Models after signing in, or the owner puts one in
  `members/<member>.env` (`RAVEN_PROVIDER`, `RAVEN_API_KEY`). A Claude
  subscription is signed in per member through the Claude CLI inside their
  container, `./family.sh exec <member> provider login claude-subscription`;
  `CLAUDE_CONFIG_DIR` points into the member's data, so the sign-in outlives
  the container. A household sharing one subscription shares its limits.
- **A sign-in of their own.** Each member's Raven names its cookie after its own
  port, so two members signed in on one shared tablet do not sign each other out.
- **Settings of their own**, the theme included (dark unless they pick another).

## Disk space

Without `--size`, a member's data directory grows as far as the server's disk.
`./family.sh add NAME --size 20G` (as root) makes a 20 GB file, formats it and
mounts it on that member's data directory, and adds it to `/etc/fstab` so it is
mounted after a reboot. Their Raven then cannot use more than that; `list`
shows the limit.

## The owner

The person with a shell on the server is the household's administrator: they
add and remove members, mint sign-in links, read logs (`./family.sh logs
NAME`), and can read every member's data on disk. Inside a member's Raven there
is no admin above the member -- it is theirs.

`./family.sh remove NAME` stops a member and forgets them but keeps their data;
`--purge` deletes it too, after asking for the name to be typed.

## Exposure

Ports listen on the home network (`FAMILY_BIND=0.0.0.0`); auto-login is always
off, so only someone with a sign-in link gets in. Reach them from outside the
home through a VPN (Tailscale, WireGuard) rather than by forwarding ports on the
router. Plain HTTP is fine inside the home network; anything wider needs an
HTTPS reverse proxy in front, and the page switches to `wss://` on its own.

## Backups and updates

Everything a member keeps is in `data/<member>/`. Stop them (`./family.sh down
NAME`), copy the directory, start them again. To update: pull the checkout,
`./family.sh build`, `./family.sh up` -- the data is untouched.
