#!/usr/bin/env python3
"""
cp_dns — read (and optionally write) an A record in a cPanel-hosted DNS zone.

    cp_dns.py cloud                → prints current IP of cloud.example.org
    cp_dns.py -w cloud 1.2.3.4     → sets it (only with -w; never writes otherwise)
    cp_dns.py -l                   → list all records in the zone
    cp_dns.py -d cloud             → debug: show what it's matching against
    cp_dns.py --raw                → dump the raw JSON from cPanel

Configuration and credentials live in a shell-style file, searched for in:
    the path given by --config
    $CP_DNS_TOKEN_FILE
    /usr/local/etc/cp_dns_token
    /etc/cp_dns_token
    ~/.cp_dns_token

Example (chmod 600 it):

    CP_USER=mycpaneluser
    CP_TOKEN=xxxxxxxxxxxx
    CP_TOKEN_NAME=DDNS_Update          # documentation only
    CP_EXPIRY=2027-01-31               # documentation only

The zone, cPanel host and other settings are read from the same file, or
from the environment; see cp_dns_token.example.

Only needs the Python standard library — no jq, no requests.
"""

import argparse
import base64
import datetime
import json
import os
import re
import sys
import urllib.parse
import urllib.request

CP_HOST = os.environ.get("CP_HOST", "cpanel.example.org")
CP_PORT = os.environ.get("CP_PORT", "2083")
ZONE    = os.environ.get("ZONE", "example.org")
TTL     = int(os.environ.get("TTL", "300"))

# Where to look for credentials, in order. The first file that exists wins.
# Override with --config, or the CP_DNS_TOKEN_FILE environment variable.
#
# System locations come first, deliberately. A credentials file tucked away
# in someone's home directory is invisible to whoever inherits the system:
# it gets forgotten, then something breaks weeks later and nobody can see
# why. Being awkward to write is a feature here, not a cost.
TOKEN_SEARCH_PATH = [
    "/usr/local/etc/cp_dns_token",
    "/etc/cp_dns_token",
    os.path.expanduser("~/.cp_dns_token"),
]

# Warn this many days before the date in CP_EXPIRY.
EXPIRY_WARN_DAYS = int(os.environ.get("EXPIRY_WARN_DAYS", "30"))


def find_token_file(explicit=None):
    """Resolve which credentials file to use.

    Precedence: --config flag, then $CP_DNS_TOKEN_FILE, then each entry
    of TOKEN_SEARCH_PATH in turn.

    An explicitly-named file that does not exist is an error rather than a
    fall-through, so a typo in a scheduled job fails loudly instead of
    silently picking up someone else's credentials.
    """
    for source, path in (
            ("--config", explicit),
            ("$CP_DNS_TOKEN_FILE", os.environ.get("CP_DNS_TOKEN_FILE")),
    ):
        if path:
            if not os.path.isfile(path):
                die("credentials file %r (given by %s) does not exist"
                    % (path, source))
            return path

    for path in TOKEN_SEARCH_PATH:
        if os.path.isfile(path):
            return path

    die("no credentials file found. Looked in:\n"
        + "".join("            %s\n" % p for p in TOKEN_SEARCH_PATH)
        + "        Copy cp_dns_token.example to one of those and fill it in,\n"
          "        or name one explicitly with --config / $CP_DNS_TOKEN_FILE.")


def die(msg, code=1):
    print("cp_dns: %s" % msg, file=sys.stderr)
    sys.exit(code)


PLACEHOLDERS = {
    "your_cpanel_username", "the_token_from_cpanel",
}


def load_creds(token_file):
    """Parse the shell-style token file, with friendly errors.

    Distinguishes 'value absent', 'value empty', and 'value is still the
    example placeholder' — because the resulting auth failure otherwise
    looks identical and sends people hunting in the wrong place.
    """
    mode = os.stat(token_file).st_mode & 0o077
    if mode:
        print("cp_dns: warning: %s is readable by others; chmod 600 it"
              % token_file, file=sys.stderr)

    cfg = {}
    with open(token_file) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")

    for key in ("CP_USER", "CP_TOKEN"):
        val = cfg.get(key)
        if val is None:
            die("%s is not set in %s\n"
                "        Add a line:  %s=..." % (key, token_file, key))
        if not val.strip():
            die("%s is empty in %s" % (key, token_file))
        if val.strip().lower() in PLACEHOLDERS:
            die("%s in %s is still the example value (%r).\n"
                "        Replace it with your real %s."
                % (key, token_file, val,
                   "cPanel username" if key == "CP_USER" else "API token"))

    # Optional documentation field: warn as expiry approaches.
    expiry = cfg.get("CP_EXPIRY", "").strip()
    if expiry:
        try:
            exp = datetime.datetime.strptime(expiry, "%Y-%m-%d").date()
            days = (exp - datetime.date.today()).days
            if days < 0:
                print("cp_dns: WARNING: token expired %s (%d days ago) — "
                      "calls will fail until it is renewed"
                      % (expiry, -days), file=sys.stderr)
            elif days <= EXPIRY_WARN_DAYS:
                print("cp_dns: WARNING: token expires %s (%d days) — "
                      "renew it in cPanel > Security > API Tokens"
                      % (expiry, days), file=sys.stderr)
        except ValueError:
            print("cp_dns: warning: CP_EXPIRY=%r is not YYYY-MM-DD" % expiry,
                  file=sys.stderr)

    # Settings may live in the same file; environment still wins.
    global CP_HOST, CP_PORT, ZONE, TTL
    CP_HOST = os.environ.get("CP_HOST") or cfg.get("CP_HOST") or CP_HOST
    CP_PORT = os.environ.get("CP_PORT") or cfg.get("CP_PORT") or CP_PORT
    ZONE    = os.environ.get("ZONE")    or cfg.get("ZONE")    or ZONE
    if os.environ.get("TTL") or cfg.get("TTL"):
        try:
            TTL = int(os.environ.get("TTL") or cfg.get("TTL"))
        except ValueError:
            pass

    if ZONE == "example.org":
        die("ZONE is still 'example.org'.\n"
            "        Set ZONE (and CP_HOST) in %s" % token_file)

    return cfg


def api(cfg, endpoint, params=None, debug=False):
    """Call a UAPI endpoint and return the decoded JSON."""
    url = "https://%s:%s/execute/%s" % (CP_HOST, CP_PORT, endpoint)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if debug:
        print("--- GET %s" % url, file=sys.stderr)
    req = urllib.request.Request(url)
    req.add_header("Authorization",
                   "cpanel %s:%s" % (cfg["CP_USER"], cfg["CP_TOKEN"]))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001
        die("request failed: %s" % exc)
    try:
        return json.loads(body)
    except ValueError:
        die("response was not JSON:\n%s" % body[:500])


def b64(value):
    """Decode a base64 field, tolerating missing padding."""
    if value is None:
        return ""
    pad = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + pad).decode("utf-8", "replace")
    except Exception:                              # noqa: BLE001
        return value


def short_name(name, zone):
    """cPanel stores names relative to the zone: 'cloud', not the FQDN.

    Accept either form from the user and normalise to what cPanel uses.
    """
    n = name.rstrip(".")
    z = zone.rstrip(".")
    if n.lower() == z.lower():
        return z + "."          # apex records are stored fully-qualified
    if n.lower().endswith("." + z.lower()):
        n = n[: -(len(z) + 1)]
    return n


def get_records(cfg, debug=False):
    data = api(cfg, "DNS/parse_zone", {"zone": ZONE}, debug=debug)
    if data.get("errors"):
        die("cPanel error: %s" % data["errors"])
    return data.get("data", [])


def find_a_record(records, want):
    """Return the matching A record dict, or None."""
    for rec in records:
        if rec.get("type") != "record":
            continue
        if rec.get("record_type") != "A":
            continue
        if (rec.get("dname_raw") or "").lower() == want.lower():
            return rec
    return None


def cmd_list(records):
    for rec in records:
        if rec.get("type") != "record":
            continue
        vals = " ".join(b64(v) for v in rec.get("data_b64", []))
        print("%-4s %-28s %-6s %-7s %s" % (
            rec.get("line_index"),
            rec.get("dname_raw", ""),
            rec.get("record_type", ""),
            rec.get("ttl", ""),
            vals))


def cmd_read(records, want, debug=False):
    if debug:
        names = [r.get("dname_raw") for r in records
                 if r.get("type") == "record" and r.get("record_type") == "A"]
        print("--- looking for A record named: %r" % want, file=sys.stderr)
        print("--- A records present: %r" % names, file=sys.stderr)
    rec = find_a_record(records, want)
    if rec is None:
        die("no A record named %r in zone %s" % (want, ZONE))
    print(b64(rec.get("data_b64", [""])[0]))


def cmd_write(cfg, records, want, newip, debug=False):
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", newip):
        die("%r does not look like an IPv4 address" % newip)

    rec = find_a_record(records, want)
    if rec is None:
        die("no A record named %r in zone %s — create it first" % (want, ZONE))

    current = b64(rec.get("data_b64", [""])[0])
    if current == newip:
        print("unchanged: %s already %s" % (want, newip))
        return

    # mass_edit_zone needs the zone's current serial for concurrency control.
    serial = None
    for r in records:
        if r.get("record_type") == "SOA":
            serial = b64(r.get("data_b64", [None, None, None])[2])
            break
    if serial is None:
        die("could not determine zone serial")

    edit = {
        "line_index": rec["line_index"],
        "dname": want,
        "ttl": TTL,
        "record_type": "A",
        "data": [newip],
    }
    params = {
        "zone": ZONE,
        "serial": serial,
        "edit": json.dumps(edit),
    }
    if debug:
        print("--- edit payload: %s" % json.dumps(edit), file=sys.stderr)
        print("--- serial: %s" % serial, file=sys.stderr)

    resp = api(cfg, "DNS/mass_edit_zone", params, debug=debug)
    if resp.get("status") == 1:
        print("updated: %s %s -> %s" % (want, current, newip))
    else:
        print(json.dumps(resp, indent=2), file=sys.stderr)
        die("update failed")


def main():
    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-w", "--write", action="store_true",
                    help="write the record (otherwise read-only)")
    ap.add_argument("-l", "--list", action="store_true",
                    help="list all records in the zone")
    ap.add_argument("-d", "--debug", action="store_true",
                    help="show diagnostic detail on stderr")
    ap.add_argument("--raw", action="store_true",
                    help="dump raw JSON from cPanel and exit")
    ap.add_argument("--tokens", action="store_true",
                    help="list API tokens and their expiry (may be refused "
                         "when authenticating with a token)")
    ap.add_argument("-c", "--config", metavar="FILE",
                    help="credentials file (default: first of %s)"
                         % ", ".join(TOKEN_SEARCH_PATH))
    ap.add_argument("name", nargs="?", help="record name, e.g. 'cloud'")
    ap.add_argument("ip", nargs="?", help="new IPv4 address (with -w)")
    args = ap.parse_args()

    token_file = find_token_file(args.config)
    if args.debug:
        print("--- credentials from: %s" % token_file, file=sys.stderr)
    cfg = load_creds(token_file)

    if args.tokens:
        data = api(cfg, "Tokens/list_tokens", debug=args.debug)
        if data.get("errors"):
            die("cPanel refused: %s\n"
                "        (token auth often cannot query token metadata; "
                "rely on CP_EXPIRY in the credentials file instead)"
                % data["errors"])
        toks = data.get("data", {})
        if isinstance(toks, dict):
            toks = list(toks.values())
        for t in toks or []:
            exp = t.get("expires_at") or t.get("expires") or "never"
            if isinstance(exp, (int, float)) or (
                    isinstance(exp, str) and exp.isdigit()):
                exp = datetime.datetime.fromtimestamp(
                    int(exp)).strftime("%Y-%m-%d")
            print("%-30s expires: %s" % (t.get("name", "?"), exp))
        return

    if args.raw:
        print(json.dumps(api(cfg, "DNS/parse_zone", {"zone": ZONE},
                             debug=args.debug), indent=2))
        return

    records = get_records(cfg, debug=args.debug)

    if args.list:
        cmd_list(records)
        return

    if not args.name:
        ap.error("a record name is required (e.g. 'cloud')")

    want = short_name(args.name, ZONE)

    if args.write:
        if not args.ip:
            ap.error("-w requires an IP address")
        cmd_write(cfg, records, want, args.ip, debug=args.debug)
    else:
        cmd_read(records, want, debug=args.debug)


if __name__ == "__main__":
    main()
