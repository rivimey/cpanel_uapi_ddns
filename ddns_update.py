#!/usr/bin/env python3
"""
ddns_update — keep a cPanel DNS A record pointed at this site's public IP.

Designed to run from Synology Task Scheduler every 10 minutes.

Behaviour:
  * Asks ONE public-IP service per run, cycling through the list, so each
    service is polled about every 30 minutes rather than every 10.
  * Compares against a locally cached value. If unchanged: exits immediately,
    making no call to cPanel at all. This is the overwhelmingly common case.
  * If a change is seen, CORROBORATES it with the next service in the cycle
    before acting. Two independent sources must agree before DNS is touched.
    Disagreement is logged and retried next cycle (a real change will still
    be there in 10 minutes); persistent disagreement raises a notification.
  * Once every RECONCILE_HOURS, reads the record back from cPanel regardless,
    so hand edits or silently-failed writes cannot leave the cache lying.

Usage:
    ddns_update.py cloud             # normal scheduled run
    ddns_update.py cloud --dry-run   # say what it would do, change nothing
    ddns_update.py cloud --force     # skip the cache, reconcile now
    ddns_update.py cloud --status    # print cached state and exit

Config lives next to cp_dns.py; see CONFIG below.
Only uses the Python standard library (works on DSM's Python 3.8).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

# ---------------------------------------------------------------- CONFIG ---

# Where cp_dns.py lives, and the interpreter to run it with.
# NOTE: absolute paths deliberately — DSM's non-interactive PATH is not
# the same as your login shell's, which has bitten this project before.
PYTHON  = os.environ.get("DDNS_PYTHON", "/usr/bin/python3")
CP_DNS  = os.environ.get("DDNS_CP_DNS",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "cp_dns.py"))

# State and log locations.
# Where to keep the cached address and the log.
#
# Deliberately not beside the script: that may live on a read-only or
# package-managed partition. /var/cache is the right home for this - the
# cache is genuinely discardable, since a lost state file simply means the
# next run reconciles against cPanel and carries on.
#
# Each falls back to the next if it cannot be written, so the script still
# works when run as an unprivileged user with no /var access.
# /var/tmp rather than /tmp as the last resort: /tmp is cleared on reboot on
# many systems, and while losing the cache is harmless, it would mean an
# extra cPanel call after every restart for no reason. /var/tmp persists.
STATE_DIRS = [
    "/var/cache/ddns_update",
    os.path.expanduser("~/.cache/ddns_update"),
    "/var/tmp/ddns_update",
    tempfile.gettempdir(),
]
LOG_DIRS = [
    "/var/log",
    os.path.expanduser("~/.cache/ddns_update"),
    "/var/tmp/ddns_update",
    tempfile.gettempdir(),
]

# Credentials file to hand to cp_dns.py. Leave empty to let cp_dns use its
# own search path. Set this when running from Task Scheduler, where $HOME
# may not be what you expect.
TOKEN_FILE = os.environ.get("DDNS_TOKEN_FILE", "")

# Public-IP services. All must return a BARE IPv4 address as plain text.
# Add or remove freely; the script cycles through them one per run.
IP_SOURCES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

# How often to re-read the record from cPanel even when nothing looks changed.
RECONCILE_HOURS = 24

# Notify after this many consecutive failures / disagreements.
ALERT_AFTER = 3

# DSM notification helper (present on Synology; ignored elsewhere).
SYNODSMNOTIFY = "/usr/syno/bin/synodsmnotify"

IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def _writable_dir(candidates):
    """First candidate directory we can actually create and write into."""
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_probe")
            with open(probe, "w") as fh:
                fh.write("")
            os.unlink(probe)
            return d
        except OSError:
            continue
    return tempfile.gettempdir()


def state_path(record):
    """State file for one record. Per-record, so several can run safely."""
    override = os.environ.get("DDNS_STATE")
    if override:
        return override
    return os.path.join(_writable_dir(STATE_DIRS), "%s.json" % record)


def log_path():
    override = os.environ.get("DDNS_LOG")
    if override:
        return override
    d = _writable_dir(LOG_DIRS)
    return os.path.join(d, "ddns_update.log")

# ------------------------------------------------------------- utilities ---


LOG_FILE = None      # resolved at startup
STATE_FILE = None    # resolved at startup


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    if not LOG_FILE:
        return
    try:
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def notify(title, msg):
    """Raise a DSM desktop/email notification, if we're on a Synology."""
    if not os.path.exists(SYNODSMNOTIFY):
        log("NOTIFY (no synodsmnotify): %s — %s" % (title, msg))
        return
    try:
        subprocess.run([SYNODSMNOTIFY, "@administrators", title, msg],
                       timeout=30, check=False)
    except Exception as exc:                              # noqa: BLE001
        log("notification failed: %s" % exc)


def valid_ip(text):
    """Return the IP if text is a well-formed IPv4 address, else None.

    This is the safety gate: anything that is not an IP — an HTML error
    page, a rate-limit message, an empty body — is rejected here rather
    than being written into DNS.
    """
    if not text:
        return None
    text = text.strip()
    m = IPV4_RE.match(text)
    if not m:
        return None
    if any(int(o) > 255 for o in m.groups()):
        return None
    return text


def fetch_ip(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gers-ddns/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return valid_ip(resp.read().decode("utf-8", "replace"))
    except Exception as exc:                              # noqa: BLE001
        log("  source %s failed: %s" % (url, exc))
        return None


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    if DRY_RUN:
        return
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        log("could not write state file: %s" % exc)


def cp_dns(args):
    """Run cp_dns.py with the given args; return (rc, stdout, stderr)."""
    cmd = [PYTHON, CP_DNS]
    if TOKEN_FILE:
        cmd += ["--config", TOKEN_FILE]
    cmd += args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:                              # noqa: BLE001
        return 1, "", str(exc)


# ------------------------------------------------------------------ main ---


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen; change nothing")
    ap.add_argument("--force", action="store_true",
                    help="ignore the cache and reconcile with cPanel now")
    ap.add_argument("--status", action="store_true",
                    help="print cached state and exit")
    ap.add_argument("record", nargs="?",
                    help="record to keep updated, e.g. 'cloud' "
                         "(or set $DDNS_RECORD)")
    args = ap.parse_args()

    record = args.record or os.environ.get("DDNS_RECORD", "")
    if not record:
        ap.error("a record name is required, e.g. 'cloud' "
                 "(or set $DDNS_RECORD)")

    global LOG_FILE, STATE_FILE
    STATE_FILE = state_path(record)
    LOG_FILE = log_path()

    state = load_state()

    if args.status:
        print(json.dumps(state, indent=2))
        return 0

    cached_ip   = state.get("ip")
    cursor      = state.get("cursor", 0)
    last_recon  = state.get("last_reconcile", 0)
    fail_count  = state.get("consecutive_failures", 0)

    # ---- pick this run's source, and advance the cursor for next time ----
    primary_idx   = cursor % len(IP_SOURCES)
    secondary_idx = (cursor + 1) % len(IP_SOURCES)
    state["cursor"] = (cursor + 1) % len(IP_SOURCES)

    observed = fetch_ip(IP_SOURCES[primary_idx])
    if observed is None:
        fail_count += 1
        state["consecutive_failures"] = fail_count
        save_state(state)
        log("could not determine public IP (failure %d)" % fail_count)
        if fail_count == ALERT_AFTER:
            notify("DDNS: cannot determine public IP",
                   "%d consecutive failures reaching IP lookup services."
                   % fail_count)
        return 1

    # a successful lookup clears the failure counter
    state["consecutive_failures"] = 0

    # ---- is a periodic reconcile due? ------------------------------------
    due = args.force or (time.time() - last_recon) > RECONCILE_HOURS * 3600
    if due or cached_ip is None:
        rc, out, err = cp_dns([record])
        if rc != 0:
            log("reconcile: cp_dns read failed: %s" % (err or out))
            notify("DDNS: cannot read DNS record",
                   "Reading %s from cPanel failed: %s" % (record, err or out))
            save_state(state)
            return 1
        dns_ip = valid_ip(out)
        log("reconcile: DNS says %s, cache said %s" % (dns_ip, cached_ip))
        if dns_ip != cached_ip:
            log("reconcile: correcting cache to match DNS")
        cached_ip = dns_ip
        state["ip"] = cached_ip
        state["last_reconcile"] = int(time.time())

    # ---- the common case: nothing has changed ----------------------------
    if observed == cached_ip:
        state["last_check"] = int(time.time())
        save_state(state)
        return 0

    # ---- a change is claimed: corroborate before acting ------------------
    log("possible change: %s says %s (cached %s) — corroborating"
        % (IP_SOURCES[primary_idx], observed, cached_ip))

    second = fetch_ip(IP_SOURCES[secondary_idx])
    if second is None:
        log("corroboration unavailable; deferring to next run")
        save_state(state)
        return 0

    if second != observed:
        # Two sources disagree. Could be a change caught mid-flight, or
        # something wrong. Either way, do not write. A genuine change will
        # still be there next run, when both should agree.
        disagreements = state.get("disagreements", 0) + 1
        state["disagreements"] = disagreements
        log("sources disagree: %s=%s vs %s=%s (count %d) — not writing"
            % (IP_SOURCES[primary_idx], observed,
               IP_SOURCES[secondary_idx], second, disagreements))
        if disagreements == ALERT_AFTER:
            notify("DDNS: IP sources disagree",
                   "Repeated disagreement between IP lookup services: "
                   "%s vs %s. DNS has not been changed."
                   % (observed, second))
        save_state(state)
        return 0

    state["disagreements"] = 0

    # ---- both agree: update DNS ------------------------------------------
    if args.dry_run:
        log("DRY RUN: would set %s to %s (was %s)"
            % (record, observed, cached_ip))
        return 0

    rc, out, err = cp_dns(["-w", record, observed])
    if rc != 0:
        log("update FAILED: %s" % (err or out))
        notify("DDNS: DNS update failed",
               "Could not update %s to %s. %s" % (record, observed, err or out))
        save_state(state)
        return 1

    log("updated %s: %s -> %s" % (record, cached_ip, observed))
    notify("DDNS: address updated",
           "%s now points to %s (was %s)." % (record, observed, cached_ip))

    state["ip"] = observed
    state["last_check"] = int(time.time())
    state["last_change"] = int(time.time())
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
