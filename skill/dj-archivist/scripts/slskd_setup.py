#!/usr/bin/env python3
"""slskd one-time setup and start for dj-archivist.

What it does:
  1. Asks for your Soulseek username and password (the password is never echoed and is stored only in slskd.yml).
  2. Generates the web UI password and the API key itself, and shows the web UI password once.
  3. Writes slskd.yml at the config's slskd.yml_path (mode 600): downloads go to <work_dir>/incoming, unfinished
     ones to <work_dir>/incoming/incomplete, and the share is a separate folder, <work_dir>/share.
  4. Starts slskd detached (log: <state_dir>/slskd.log) and waits until it is logged in to Soulseek.

Usage:
  python3 slskd_setup.py [--config PATH]             set up, then start
  python3 slskd_setup.py [--config PATH] --start     start only (slskd.yml already written)
  python3 slskd_setup.py [--config PATH] --status    is slskd running and logged in?

A Soulseek account is created at its first login: a free username becomes yours with the password you typed; if
the name is taken and the password does not match, the login fails.
Share only what you are allowed to redistribute (for example Creative Commons material); keep the archive out of
the share folder.
"""
import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archivist  # noqa: E402  (shares the config loading with the loop)


def raise_open_files_limit(target=10240):
    """slskd opens hundreds of files and peer connections; a GUI-launched process on macOS may start with a soft
    limit of 256 open files, which ends in "Too many open files" and crashes. Raise the soft limit for slskd."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))


def q(s):
    """YAML single-quoted scalar."""
    return "'" + str(s).replace("'", "''") + "'"


def api_key(cfg):
    if cfg.api_key:
        return cfg.api_key
    m = re.search(r"^\s*key:\s*'?([^'\s]+)'?", cfg.yml.read_text(errors="replace"), re.M) if cfg.yml.exists() else None
    return m.group(1) if m else None


def status(cfg):
    try:
        req = urllib.request.Request(f"{cfg.slskd_url}/api/v0/server", headers={"X-API-Key": api_key(cfg) or ""})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def ask_credentials():
    user = input("Soulseek username: ").strip()
    password = getpass.getpass("Soulseek password (not shown): ")
    if not user or not password:
        sys.exit("username and password must not be empty")
    return user, password


def write_yml(cfg, user, password):
    web_password = secrets.token_urlsafe(12)
    key = secrets.token_hex(32)
    for d in (cfg.incomplete, cfg.share):
        d.mkdir(parents=True, exist_ok=True)
    port = urllib.parse.urlsplit(cfg.slskd_url).port or 5030
    text = f"""# Written by dj-archivist slskd_setup.py. Holds credentials (mode 600): never commit or share it.
directories:
  downloads: {q(cfg.incoming)}
  incomplete: {q(cfg.incomplete)}
shares:
  directories:
    - {q(cfg.share)}
soulseek:
  username: {q(user)}
  password: {q(password)}
web:
  port: {port}
  authentication:
    username: slskd
    password: {q(web_password)}
    api_keys:
      dj-archivist:
        key: {key}
        role: readwrite
"""
    cfg.yml.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.yml, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # never readable by others, not even briefly
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(cfg.yml, 0o600)
    print(f"\n{cfg.yml} written. Web UI ({cfg.slskd_url}) login: user slskd, password {web_password}")
    print("(note the web UI password somewhere; archivist.py reads the API key from slskd.yml itself)\n")
    if cfg.api_key:
        print("note: an API key is set in the config (slskd.api_key or $SLSKD_API_KEY) and overrides the new key in "
              "slskd.yml; clear it or update it")


def start(cfg):
    """Start slskd detached unless it already answers, then wait for the Soulseek login. True when logged in."""
    if "error" not in status(cfg):
        print("slskd is already running")
    else:
        if not cfg.slskd_binary.exists():
            sys.exit(f"slskd binary not found: {cfg.slskd_binary} (set slskd.binary in the config)")
        if not cfg.yml.exists():
            sys.exit(f"{cfg.yml} not found: run slskd_setup.py without --start first")
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.slskd_log, "a") as log:
            subprocess.Popen([str(cfg.slskd_binary), "--config", str(cfg.yml)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                             preexec_fn=raise_open_files_limit)
        print("slskd started, waiting for the Soulseek login...")
    s = {}
    for _ in range(40):
        time.sleep(1.5)
        s = status(cfg)
        state = str(s.get("state", ""))
        if "LoggedIn" in state:
            print(f"OK: logged in to Soulseek ({state}). Next: python3 archivist.py start")
            return True
    print(f"no login seen, last status: {s}")
    if cfg.slskd_log.exists():
        print(f"last lines of {cfg.slskd_log}:")
        print("\n".join(cfg.slskd_log.read_text(errors="ignore").splitlines()[-15:]))
    print("\nWrong password? Run slskd_setup.py again (set up anew and try the other password).")
    return False


def stop_running(cfg):
    """A running slskd would keep its old config: stop it and wait until it no longer answers."""
    subprocess.run(["pkill", "-f", f"{cfg.slskd_binary} --config"], capture_output=True)
    for _ in range(20):
        if "error" in status(cfg):
            return
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", help="config JSON (default: $DJ_ARCHIVIST_CONFIG, else ~/.dj-archivist/config.json)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--start", action="store_true", help="start slskd only (slskd.yml already written)")
    mode.add_argument("--status", action="store_true", help="print the slskd server status")
    a = ap.parse_args()
    cfg = archivist.load_config(a.config)
    if a.status:
        print(json.dumps(status(cfg), indent=2))
        return
    if not a.start:
        user, password = ask_credentials()
        stop_running(cfg)
        write_yml(cfg, user, password)
    sys.exit(0 if start(cfg) else 1)


if __name__ == "__main__":
    main()
