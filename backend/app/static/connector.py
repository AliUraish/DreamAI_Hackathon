#!/usr/bin/env python3
"""Chowkidaar connector. Run it inside your project:

    curl -fsSL <backend>/api/v1/connector.py | CHOWKIDAAR_API_KEY=ck_live_... python3 - --watch

It tells Chowkidaar where the project is (git remote, branch, path) and which
credential-like environment variables it has. Values never leave this machine:
each one is reduced here to HMAC-SHA256(workspace salt, value), and only the first
16 hex characters of that are sent, next to the variable's name. Standard library only.
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The old variable names (from before the rename) are still read, so a command copied earlier keeps working.
BACKEND = (os.environ.get("CHOWKIDAAR_URL") or os.environ.get("PATCH" + "LAYER_URL") or "__BACKEND__").rstrip("/")
API_KEY = os.environ.get("CHOWKIDAAR_API_KEY") or os.environ.get("PATCH" + "LAYER_API_KEY") or ""
ENV_FILES = [".env.local", ".env", ".env.development", ".env.production", ".env.example", ".env.sample", ".env.template"]
PLACEHOLDER = re.compile(r"^(|x+|\.+|<.*>|\$\{.*\}|your[-_ ].*|.*your[-_]?(api[-_]?)?key.*|changeme|change[-_]me|todo|replace[-_]?me|placeholder|example|null|none|sk-\.\.\.)$", re.I)
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def call(path, body=None):
    request = urllib.request.Request(BACKEND + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"authorization": "Bearer " + API_KEY, "content-type": "application/json"},
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="ignore")
        sys.exit("Chowkidaar refused the request (%s): %s" % (error.code, detail[:300]))
    except urllib.error.URLError as error:
        sys.exit("Chowkidaar is not reachable at %s: %s" % (BACKEND, error.reason))


def git(*args):
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def read_env(root, prefixes, salt):
    """name -> {fingerprint, files, hint}. The value is used on the next three lines and nowhere else."""
    entries = {}
    for filename in ENV_FILES:
        path = os.path.join(root, filename)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                match = LINE.match(raw)
                if not match or raw.lstrip().startswith("#"):
                    continue
                name, value = match.group(1), match.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].strip()
                entry = entries.setdefault(name, {"name": name, "fingerprint": None, "files": [], "hint": None})
                entry["files"].append(filename)
                if entry["fingerprint"] is None and not PLACEHOLDER.match(value):
                    entry["fingerprint"] = hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]
                    entry["hint"] = next((provider for provider, starts in prefixes.items() if any(value.startswith(s) for s in starts)), None)
    return sorted(entries.values(), key=lambda e: e["name"])


def main():
    if not API_KEY:
        sys.exit("Set CHOWKIDAAR_API_KEY (you got it during onboarding).")
    root = git("rev-parse", "--show-toplevel") or os.getcwd()
    if not os.path.isdir(os.path.join(root, ".git")):
        sys.exit("Run this inside a git repository.")
    config = call("/api/v1/connector/config")
    env = read_env(root, config["key_prefixes"], config["fingerprint_salt"])
    result = call("/api/v1/connect", {"local_path": root, "remote_url": git("-C", root, "remote", "get-url", "origin"),
                                      "branch": git("-C", root, "rev-parse", "--abbrev-ref", "HEAD"), "env": env})
    print("Connected %s to Chowkidaar (%d variables fingerprinted, no values sent)." % (result["name"], len([e for e in env if e["fingerprint"]])))
    print("Open %s" % result["url"])
    if "--watch" not in sys.argv:
        return
    print("Watching env files. Ctrl+C to stop.")
    last = json.dumps(env, sort_keys=True)
    while True:
        time.sleep(3)
        env = read_env(root, config["key_prefixes"], config["fingerprint_salt"])
        current = json.dumps(env, sort_keys=True)
        if current != last:
            last = current
            changes = call("/api/v1/env", {"repo_id": result["repo_id"], "env": env}).get("changes", [])
            for change in changes:
                print("  -> %s" % change)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
