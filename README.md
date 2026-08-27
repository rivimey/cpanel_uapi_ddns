# cp_dns — dynamic DNS for cPanel-hosted zones

Two small Python scripts that keep a DNS `A` record pointed at a changing
home or office IP address, using cPanel's UAPI. No dependencies beyond the
Python standard library, so they run on constrained boxes — NAS units,
routers, elderly servers — without installing anything.

- **`cp_dns.py`** — read or write a single `A` record in a cPanel zone.
- **`ddns_update.py`** — the scheduled job: work out the current public IP,
  and update the record when it changes.

## Why

Plenty of hosts sell DNS but no dynamic-DNS service, and their API may not
be available on shared plans. cPanel's own Zone Editor *is* exposed through
UAPI on most shared accounts, which is enough to build dynamic DNS on
without moving your nameservers anywhere.

## Requirements

- Python 3.6 or later (tested on 3.8 and 3.13).
- A cPanel account with API token access.
- A DNS `A` record that already exists in the zone. These scripts update
  records; they do not create them. Create it once in the Zone Editor.

### No virtualenv, no pip, no requirements.txt

There deliberately isn't one. Every import is from the Python standard
library — `argparse`, `base64`, `datetime`, `json`, `os`, `re`,
`subprocess`, `sys`, `tempfile`, `time`, `urllib` — so there is nothing to
install and nothing for a virtualenv to isolate.

Installation is: copy the two `.py` files somewhere, and run them.

This is a deliberate constraint rather than an accident. `requests` would
make the HTTP code prettier, but it would also turn deployment on a NAS or
router into an exercise in getting `pip` working against a vendor Python that
may be several years old and may not permit system-wide installs. For a
script whose job is to run unattended for years on whatever hardware
happens to be there, having no dependencies is worth more than tidier
syntax.

If you extend these scripts, keeping that property is worth some effort.

## Setup

**1. Create an API token.** In cPanel: *Security → Manage API Tokens →
Create*. Name it something a successor will recognise. Set an expiry date —
and write that date down, because the token is shown only once.

**2. Write the configuration file.**

```sh
sudo cp cp_dns_token.example /usr/local/etc/cp_dns_token
sudo chmod 600 /usr/local/etc/cp_dns_token
sudo $EDITOR /usr/local/etc/cp_dns_token
```

Set `CP_USER`, `CP_TOKEN`, `ZONE` and `CP_HOST`. Recording `CP_TOKEN_NAME`
and `CP_EXPIRY` is optional but strongly advised: `cp_dns` warns as the
expiry approaches, and an expired token otherwise makes DNS updates fail
silently.

Note `CP_USER` is your *cPanel* username — usually a short string, not the
email address you use for your host's client area. It is shown in cPanel
under "General Information".

**3. Check it works.**

```sh
./cp_dns.py --list          # every record in the zone
./cp_dns.py cloud           # print the current IP of cloud.example.org
```

## Usage

### cp_dns.py

```sh
./cp_dns.py NAME                 # print the A record's current address
./cp_dns.py -w NAME 192.0.2.1    # set it (never writes without -w)
./cp_dns.py --list               # list the whole zone
./cp_dns.py --raw                # raw JSON from cPanel, for debugging
./cp_dns.py -d NAME              # show what it is matching against
./cp_dns.py --tokens             # list API tokens and expiry, if permitted
./cp_dns.py --config FILE NAME   # use a specific credentials file
```

`NAME` may be given either short (`cloud`) or fully qualified
(`cloud.example.org`); both are accepted.

Writing is deliberately behind an explicit `-w`, so a mistyped command
cannot change live DNS. If the record already holds the requested address,
nothing is sent.

### ddns_update.py

Meant to run from cron or an equivalent scheduler, every 10 minutes or so:

```sh
./ddns_update.py cloud              # normal run
./ddns_update.py cloud --dry-run    # report, change nothing
./ddns_update.py cloud --force      # reconcile with cPanel now
./ddns_update.py cloud --status     # show cached state
```

## How ddns_update decides

The design tries to be quiet, frugal with other people's services, and
hard to fool.

**One lookup per run, rotated.** Each run queries a single public-IP
service, cycling through the configured list. With three services and a
10-minute schedule, each is polled about twice an hour.

**Cache first.** The last known address is kept in a small state file. When
the observed address matches it — which is almost always — the script exits
without contacting cPanel at all. cPanel therefore sees a handful of calls
a year rather than thousands, and rate limits never come into play.

**Corroborate before writing.** If a lookup reports a *different* address,
the script asks the next service in the cycle before believing it. Two
independent sources must agree before live DNS is touched. This costs one
extra request on the rare occasions something changes, and guards against a
single service returning a plausible but wrong answer.

**Disagreement is not an error.** If the two sources differ, nothing is
written and it tries again next cycle — a genuine address change will still
be there in ten minutes, by which time both should agree. Only persistent
disagreement raises an alert.

**Validate everything.** Any response that is not a well-formed IPv4
address — an HTML error page, a rate-limit message, an empty body — is
discarded rather than used. This is the gate that stops a misbehaving
service writing nonsense into your zone.

**Reconcile daily.** Once every 24 hours the record is read back from
cPanel regardless of the cache, so a hand edit or a silently failed write
cannot leave the cached state permanently out of step with reality.

## Configuration

Settings are read from the credentials file, and may be overridden by
environment variables of the same name.

| Setting | Purpose |
| --- | --- |
| `CP_USER` | cPanel account username |
| `CP_TOKEN` | cPanel API token |
| `ZONE` | DNS zone, e.g. `example.org` |
| `CP_HOST` | cPanel hostname |
| `CP_PORT` | cPanel port (default `2083`) |
| `TTL` | TTL written with updated records (default `300`) |
| `CP_TOKEN_NAME` | Documentation only |
| `CP_EXPIRY` | Documentation only; drives the expiry warning |

`ddns_update.py` additionally honours:

| Variable | Purpose |
| --- | --- |
| `DDNS_RECORD` | Record name, if not given as an argument |
| `DDNS_PYTHON` | Interpreter used to run `cp_dns.py` |
| `DDNS_CP_DNS` | Path to `cp_dns.py` |
| `DDNS_STATE` | Override the state file location |
| `DDNS_LOG` | Override the log file location |
| `DDNS_TOKEN_FILE` | Credentials file passed through to `cp_dns.py` |

### State and log files

The cached address is written to the first of these that can be created
and written to:

    /var/cache/ddns_update/<record>.json
    ~/.cache/ddns_update/<record>.json
    /var/tmp/ddns_update/<record>.json
    $TMPDIR/<record>.json

and the log, similarly, to `/var/log`, then `~/.cache/ddns_update`, then
`/var/tmp/ddns_update`. Nothing is written beside the scripts themselves,
so they can be installed on a read-only or package-managed partition.

`/var/tmp` is preferred over `/tmp` as a last resort because `/tmp` is
cleared on reboot on many systems; losing the cache is harmless, but it
would mean an unnecessary cPanel call after every restart.

The state file is a cache in the proper sense: losing it costs nothing, as
the next run reconciles against cPanel and carries on.

## Scheduling

### cron

```cron
*/10 * * * * /usr/bin/python3 /opt/cp_dns/ddns_update.py cloud
```

### Synology DSM

*Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script*, repeating every 10 minutes:

```sh
/usr/bin/python3 /volume1/scripts/ddns_update.py cloud
```

Use an **absolute path to the interpreter**. DSM runs scheduled and remote
commands with a different `PATH` from your login shell, and relying on the
shebang or on `python3` being found is a good way to produce a job that
works when you test it by hand and fails silently thereafter.

Synology notifications are used for alerts if `synodsmnotify` is present.
Configure the destination under *Control Panel → Notification*.

## Multiple records

Run one scheduled job per record. State files are named after the record,
so several jobs coexist without clashing:

```sh
./ddns_update.py cloud
./ddns_update.py vpn
```

Often tidier: keep one `A` record updated, and point the others at it with
`CNAME` records. Then only one thing changes when the address does.

## Security notes

The token grants access to your cPanel account's API. Treat the
configuration file as a secret: `chmod 600`, never commit it, and prefer a
token with an expiry date so that a leaked one eventually stops working.

Take care with shell tracing. Running these scripts under `sh -x` or
`bash -x` will print the token in the trace output, along with any command
that embeds it. That output then lives in your scrollback and shell
history.

Verify TLS. The scripts use Python's default certificate verification and
do not offer a way to disable it. If you see
`CERTIFICATE_VERIFY_FAILED`, fix the certificate store rather than working
around the check — on macOS, run the `Install Certificates.command` that
ships with your Python, or `pip install --upgrade certifi`.

## Limitations

- IPv4 `A` records only. No `AAAA` support.
- Updates existing records; does not create them.
- Tested against cPanel 11.126. The `DNS::parse_zone` and
  `DNS::mass_edit_zone` UAPI functions are used; older or unusual cPanel
  builds may differ.
- `--tokens` may be refused, depending on whether your host permits token
  authentication to query token metadata. The `CP_EXPIRY` field exists
  because of this.

## Licence

MIT.
