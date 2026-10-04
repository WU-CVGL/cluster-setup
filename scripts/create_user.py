"""Create new users on the cluster.

For every user listed in a CSV file this script:

  1. creates the Linux account on the login node (password stored as a
     SHA-512 crypt hash), and adds it to the ``docker`` group unless
     ``--no-docker-group`` is given;
  2. creates the matching group, user, home dataset (with ACL) and NFS share
     on TrueNAS;
  3. adds the NFS home to /etc/fstab and mounts it on the login node
     (/workspace/<user> and /home/<user>) and on every GPU node
     (/workspace/<user>);
  4. copies /etc/skel into the new NFS home;
  5. creates the Determined AI user and links it to the Linux UID/GID;
  6. creates the Harbor user and adds it to the ``library`` project.

Every step checks first and only creates what is missing, so rerunning the
same command finishes a half-created user. The whole CSV is validated before
anything is touched.

Admin credentials come from my_secrets.py next to this script (gitignored):
TRUENAS_USERNAME, TRUENAS_PASSWORD, SUDO_PASSWORD, DET_PASSWORD,
HARBOR_PASSWORD. The SSH key passphrase, if any, comes from $SSH_PASSPHRASE.
"""

import argparse
import csv
import re
import shlex
import sys
import time
from io import StringIO
from os import getenv
from typing import NamedTuple
from urllib.parse import quote

import requests
from fabric import Connection, Config
from my_secrets import TRUENAS_USERNAME, TRUENAS_PASSWORD, SUDO_PASSWORD, DET_PASSWORD, HARBOR_PASSWORD


TRUENAS_API_URL = "http://10.0.1.70/api/v2.0"
HARBOR_API_URL = "http://10.0.1.68:50000/api/v2.0"

# SSH hosts (names from the admin's ~/.ssh/config)
LOGIN_HOST = "cvgladmin@login"
GPU_HOSTS = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]

# TrueNAS home datasets and their NFS exports
NAS_HOST = "nas.cvgl.lab"
HOME_DATASET_PARENT = "Peter/Workspace"
HOME_QUOTA = 8 * 1024**4  # 8TB
NFS_NETWORKS = ["192.168.233.0/24", "10.0.1.64/27"]
# Same options as scripts/nfs-remount.sh (docs/03, NFS client mount options). nconnect applies per NFS
# server: the mounts of one server share the connections of the first one mounted.
NFS_MOUNT_OPTIONS = "defaults,vers=3,noatime,hard,nconnect=16,rsize=1048576,wsize=1048576,_netdev"

# Harbor: project 1 is "library"; role 2 is "Developer"
HARBOR_PROJECT_ID = 1
HARBOR_ROLE_ID = 2

HTTP_TIMEOUT = (10, 120)  # (connect, read) seconds
TRUENAS_JOB_TIMEOUT = 600  # seconds
TRUENAS_JOB_POLL_INTERVAL = 2  # seconds

# A file from /etc/skel; if it exists in the home, the skeleton was already copied.
SKEL_MARKER = ".bashrc"

CSV_COLUMNS = ("username", "full_name", "password")
USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
# TrueNAS turns these query-string values into booleans/null, so lookups by name would break.
RESERVED_USERNAMES = {"true", "false", "null"}
SHA512_CRYPT_RE = re.compile(r"\$6\$(rounds=[0-9]+\$)?[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}")

# Appends line $1 to file $2 unless it is already there, first making sure the
# file ends with a newline. Run as: sh -c "$FSTAB_APPEND_SCRIPT" sh LINE FILE
FSTAB_APPEND_SCRIPT = (
    'grep -qxF -- "$1" "$2" && exit 0; '
    'if [ -n "$(tail -c 1 "$2")" ]; then echo >> "$2"; fi; '
    'printf "%s\\n" "$1" >> "$2"'
)

connect_kwargs = {
    'passphrase': getenv('SSH_PASSPHRASE')  # SSH private key passphrase
}

q = shlex.quote


class NewUser(NamedTuple):
    username: str
    full_name: str
    password: str


class StepError(Exception):
    """A step could not be completed; the message says why."""


class ApiError(StepError):
    """An HTTP API call failed."""


# ---------------------------------------------------------------------------
# Input: CSV parsing and validation
# ---------------------------------------------------------------------------

def username_problems(username):
    if not USERNAME_RE.fullmatch(username):
        return ["username must match ^[a-z_][a-z0-9_-]{0,31}$"]
    if username in RESERVED_USERNAMES:
        return ["username must not be 'true', 'false' or 'null'"]
    return []


def password_problems(password):
    # The same rules as Determined's and Harbor's password checks.
    problems = []
    if len(password) < 8:
        problems.append("password must have at least 8 characters")
    if len(password) > 128:
        problems.append("password must have at most 128 characters")
    if not re.search(r"[A-Z]", password):
        problems.append("password must include an uppercase letter")
    if not re.search(r"[a-z]", password):
        problems.append("password must include a lowercase letter")
    if not re.search(r"[0-9]", password):
        problems.append("password must include a number")
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        problems.append("password must not contain control characters (newline, tab, ...)")
    if password != password.strip():
        problems.append("password must not start or end with whitespace")
    return problems


def validate_users(users):
    """Return a list of error messages; empty if every user is valid."""
    errors = []
    if not users:
        errors.append("no users found")
    seen = set()
    for user in users:
        label = repr(user.username)
        errors.extend(f"{label}: {p}" for p in username_problems(user.username))
        if user.username in seen:
            errors.append(f"{label}: duplicate username")
        seen.add(user.username)
        if not user.full_name:
            errors.append(f"{label}: full name must not be empty")
        elif any(ord(c) < 32 or ord(c) == 127 for c in user.full_name):
            errors.append(f"{label}: full name must not contain control characters")
        errors.extend(f"{label}: {p}" for p in password_problems(user.password))
    return errors


def read_users_csv(path):
    """Parse the users CSV. Returns (users, errors)."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            kept = [(n, line) for n, line in enumerate(f, start=1)
                    if line.strip() and not line.lstrip().startswith("#")]
    except (OSError, UnicodeDecodeError) as e:
        return [], [f"cannot read {path}: {e}"]
    if not kept:
        return [], [f"{path}: no header line ({','.join(CSV_COLUMNS)})"]

    reader = csv.reader(line for _, line in kept)
    users, errors = [], []
    try:
        header = [cell.strip() for cell in next(reader)]
        if sorted(header) != sorted(CSV_COLUMNS):
            return [], [f"{path}: header must be exactly {','.join(CSV_COLUMNS)} (got {','.join(header)})"]
        for row in reader:
            lineno = kept[reader.line_num - 1][0]
            if len(row) != len(header):
                errors.append(f"{path} line {lineno}: expected {len(header)} fields, got {len(row)}")
                continue
            record = dict(zip(header, row))
            users.append(NewUser(
                username=record["username"].strip(),
                full_name=record["full_name"].strip(),
                password=record["password"],  # used exactly as written
            ))
    except csv.Error as e:
        errors.append(f"{path}: CSV error: {e}")
    return users, errors


def load_users(path):
    """Read and validate the CSV. Returns (users, errors)."""
    users, errors = read_users_csv(path)
    if errors:
        return users, errors
    return users, validate_users(users)


# ---------------------------------------------------------------------------
# HTTP APIs (TrueNAS, Harbor)
# ---------------------------------------------------------------------------

def _short(text, limit=500):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def api_request(method, url, auth, **kwargs):
    """Send one API request. Returns the decoded JSON body (None if empty)."""
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    try:
        response = requests.request(method, url, auth=auth, **kwargs)
    except requests.RequestException as e:
        raise ApiError(f"{method} {url} failed: {e}") from e
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise ApiError(f"{method} {url} returned HTTP {response.status_code}: {_short(response.text)}") from e
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as e:
        raise ApiError(f"{method} {url} returned a non-JSON body: {_short(response.text)}") from e


def truenas(method, path, **kwargs):
    return api_request(method, TRUENAS_API_URL + path, (TRUENAS_USERNAME, TRUENAS_PASSWORD), **kwargs)


def harbor(method, path, **kwargs):
    return api_request(method, HARBOR_API_URL + path, ("admin", HARBOR_PASSWORD), **kwargs)


def home_dataset(username):
    return f"{HOME_DATASET_PARENT}/{username}"


def exact_matches(items, key, value):
    """Query results whose `key` equals `value` (in case a server ignores the filter)."""
    return [item for item in items or [] if item.get(key) == value]


def wait_truenas_job(job_id, what, timeout=TRUENAS_JOB_TIMEOUT, poll_interval=TRUENAS_JOB_POLL_INTERVAL):
    """Poll a TrueNAS job until it finishes; raise unless it succeeded."""
    if isinstance(job_id, bool) or not isinstance(job_id, int):
        raise StepError(f"{what}: expected a TrueNAS job id, got {job_id!r}")
    deadline = time.monotonic() + timeout
    while True:
        jobs = truenas("GET", "/core/get_jobs", params={"id": job_id})
        if not jobs:
            raise StepError(f"{what}: TrueNAS job {job_id} not found")
        state = jobs[0].get("state")
        if state == "SUCCESS":
            return jobs[0]
        if state in ("FAILED", "ABORTED"):
            raise StepError(f"{what}: TrueNAS job {job_id} {state}: {_short(str(jobs[0].get('error')))}")
        if time.monotonic() >= deadline:
            raise StepError(f"{what}: TrueNAS job {job_id} still {state} after {timeout}s")
        time.sleep(poll_interval)


def ensure_truenas_group(username, gid):
    """Returns (group primary key, status)."""
    found = exact_matches(truenas("GET", "/group/", params={"name": username}), "name", username)
    if found:
        if found[0].get("gid") != gid:
            raise StepError(f"TrueNAS group {username} exists with gid {found[0].get('gid')}, expected {gid}")
        return found[0]["id"], "already exists"
    truenas("POST", "/group/", json={"gid": gid, "name": username, "smb": False})
    found = exact_matches(truenas("GET", "/group/", params={"name": username}), "name", username)
    if not found:
        raise StepError(f"TrueNAS group {username} not found after creating it")
    return found[0]["id"], f"created (gid={gid})"


def ensure_truenas_user(username, uid, group_pk):
    found = exact_matches(truenas("GET", "/user/", params={"username": username}), "username", username)
    if found:
        if found[0].get("uid") != uid:
            raise StepError(f"TrueNAS user {username} exists with uid {found[0].get('uid')}, expected {uid}")
        return "already exists"
    truenas("POST", "/user/", json={
        "password_disabled": True,
        "group_create": False,
        "username": username,
        "full_name": username,
        "uid": uid,
        "group": group_pk,
        "smb": False
    })
    return f"created (uid={uid})"


def ensure_home_dataset(username):
    dataset = home_dataset(username)
    if exact_matches(truenas("GET", "/pool/dataset/", params={"id": dataset}), "id", dataset):
        return "already exists"
    truenas("POST", "/pool/dataset/", json={"name": dataset, "quota": HOME_QUOTA})
    return "created"


def ensure_home_acl(username, uid, gid):
    # Reference:
    # https://github.com/truenas/middleware/blob/master/src/middlewared/middlewared/plugins/pool_/dataset_quota_and_perms.py
    # https://github.com/truenas/middleware/blob/master/tests/api2/test_345_acl_nfs4.py
    # set_default_acl=True without a "mode": TrueNAS SCALE (checked on 23.10.1, the deployed
    # version) applies its NFS4_RESTRICTED template (POSIX_RESTRICTED if the dataset's acltype
    # is POSIX) plus builtin_users (MODIFY) and builtin_administrators (FULL_CONTROL) entries;
    # an "acl" list in the request would be ignored, so none is sent. The manual procedure in docs/02 uses the NFS4_HOME preset
    # instead. pool.dataset.permission no longer exists on TrueNAS 24.10+ (filesystem.setacl).
    dataset = home_dataset(username)
    stat = truenas("POST", "/filesystem/stat/", json=f"/mnt/{dataset}")
    if stat and stat.get("uid") == uid and stat.get("gid") == gid:
        return "already applied (owner matches)"
    data = {
        "user": username,
        "group": username,
        "options": {
            "set_default_acl": True,
            "stripacl": False,
            "recursive": True,
            "traverse": True
        }
    }
    # pool.dataset.permission runs as a job: the response is only the job id.
    job_id = truenas("POST", f"/pool/dataset/id/{quote(dataset, safe='')}/permission", json=data)
    wait_truenas_job(job_id, "set home ACL")
    return "applied"


def ensure_nfs_share(username):
    path = f"/mnt/{home_dataset(username)}"
    if exact_matches(truenas("GET", "/sharing/nfs", params={"path": path}), "path", path):
        return "already exists"
    truenas("POST", "/sharing/nfs", json={"path": path, "networks": NFS_NETWORKS, "enabled": True})
    return "created"


def ensure_harbor_user(username, password):
    if exact_matches(harbor("GET", "/users", params={"q": f"username={username}"}), "username", username):
        return "already exists"
    harbor("POST", "/users", json={
        "username": username,
        "password": password,
        "realname": username,
        "email": f"{username}@example.com"
    })
    return "created"


def ensure_harbor_member(username):
    path = f"/projects/{HARBOR_PROJECT_ID}/members"
    members = harbor("GET", path, params={"entityname": username}) or []
    if any(m.get("entity_type") == "u" and m.get("entity_name") == username for m in members):
        return "already a member"
    harbor("POST", path, json={"role_id": HARBOR_ROLE_ID, "member_user": {"username": username}})
    return "added"


# ---------------------------------------------------------------------------
# SSH steps (login node, GPU nodes)
# ---------------------------------------------------------------------------

def make_connection(host):
    config = Config(overrides={'sudo': {'password': SUDO_PASSWORD}})
    return Connection(host, config=config, connect_kwargs=connect_kwargs)


def set_linux_password(conn, username, password):
    # Hash on the login node without sudo, password on stdin only (never in argv).
    result = conn.run("openssl passwd -6 -stdin", in_stream=StringIO(password + "\n"), hide=True)
    hashed = result.stdout.strip()
    if not SHA512_CRYPT_RE.fullmatch(hashed):
        raise StepError("`openssl passwd -6 -stdin` did not return a SHA-512 crypt hash")
    conn.sudo(f"usermod -p {q(hashed)} {q(username)}", hide=True)


def ensure_linux_user(conn, user, docker_group=True):
    """Returns (uid, gid, status)."""
    name = q(user.username)
    done = []
    if conn.run(f"id -u {name}", warn=True, hide=True).ok:
        done.append("account exists")
    else:
        conn.sudo(f"useradd -m -s /bin/bash {name}", hide=True)
        done.append("account created")
    uid = int(conn.run(f"id -u {name}", hide=True).stdout.strip())
    gid = int(conn.run(f"id -g {name}", hide=True).stdout.strip())

    # passwd -S: "<user> P ..." = usable password; L (locked, as after useradd) or NP = none yet.
    fields = conn.sudo(f"passwd -S {name}", hide=True).stdout.split()
    if len(fields) >= 2 and fields[1] in ("P", "PS"):
        done.append("password already set")
    else:
        set_linux_password(conn, user.username, user.password)
        done.append("password set")

    if docker_group:
        if "docker" in conn.run(f"id -nG {name}", hide=True).stdout.split():
            done.append("already in docker group")
        else:
            conn.sudo(f"usermod -aG docker {name}", hide=True)
            done.append("added to docker group")
    return uid, gid, f"{', '.join(done)} (uid={uid}, gid={gid})"


def fstab_line(source, target):
    return f"{source} {target} nfs {NFS_MOUNT_OPTIONS} 0 2"


def fstab_entries_for(fstab_text, target):
    """Non-comment fstab lines whose mount point is `target`."""
    entries = []
    for line in fstab_text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith("#") and fields[1] == target:
            entries.append(line)
    return entries


def ensure_nfs_mount(conn, source, target):
    """Add the fstab line (if missing) and mount `target`. Returns a status string."""
    line = fstab_line(source, target)
    done = []
    conn.sudo(f"mkdir -p {q(target)}", hide=True)
    fstab = conn.run("cat /etc/fstab", hide=True).stdout
    entries = fstab_entries_for(fstab, target)
    if line in entries:
        done.append("fstab entry present")
    elif entries:
        # e.g. a line written by an older version of this script: keep it, do not add a duplicate
        done.append(f"WARNING: kept existing fstab entry {entries[0]!r}")
    else:
        conn.sudo(f"sh -c {q(FSTAB_APPEND_SCRIPT)} sh {q(line)} /etc/fstab", hide=True)
        done.append("fstab entry added")
    if conn.run(f"mountpoint -q {q(target)}", warn=True, hide=True).ok:
        done.append("already mounted")
    else:
        conn.sudo(f"mount {q(target)}", hide=True)
        done.append("mounted")
    return f"{target}: {', '.join(done)}"


def mount_home_all(username, login_conn, connect=None, log=print):
    """Mount the new home on every host. Keeps going when a host fails.

    Returns a list of (host, error message) for the hosts that failed.
    """
    connect = connect or make_connection
    source = f"{NAS_HOST}:/mnt/{home_dataset(username)}"
    failures = []
    for host in [LOGIN_HOST] + GPU_HOSTS:
        targets = [f"/workspace/{username}"]
        if host == LOGIN_HOST:
            targets.append(f"/home/{username}")
        conn = login_conn if host == LOGIN_HOST else None
        try:
            if conn is None:
                conn = connect(host)
            for target in targets:
                log(f"[{username}] mount on {host}: {ensure_nfs_mount(conn, source, target)}")
        except Exception as e:
            message = describe_error(e)
            log(f"[{username}] mount on {host}: FAILED: {message}")
            failures.append((host, message))
        finally:
            if conn is not None and conn is not login_conn:
                conn.close()
    return failures


def ensure_home_contents(conn, username):
    home = f"/home/{username}"
    if not conn.run(f"mountpoint -q {q(home)}", warn=True, hide=True).ok:
        raise StepError(f"{home} is not mounted on the login node; fix the mount and rerun")
    if conn.sudo(f"test -e {q(home + '/' + SKEL_MARKER)}", user=username, warn=True, hide=True).ok:
        return f"already populated ({SKEL_MARKER} exists)"
    conn.sudo("xdg-user-dirs-update --force", user=username, hide=True)
    conn.sudo(f"cp -a /etc/skel/. {q(home + '/')}", user=username, hide=True)
    return "populated from /etc/skel"


def parse_det_usernames(text):
    """Usernames from the table printed by `det user list`."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        header = [cell.strip() for cell in line.split("|")]
        if "Username" in header:
            column = header.index("Username")
            names = set()
            for row in lines[i + 1:]:
                if not row.strip() or set(row.strip()) <= set("-+| "):
                    continue  # separator line
                cells = [cell.strip() for cell in row.split("|")]
                if len(cells) > column:
                    names.add(cells[column])
            return names
    raise StepError("could not find the Username column in `det user list` output")


def ensure_det_user(conn, user, uid, gid):
    name = q(user.username)
    conn.run("det user login admin", in_stream=StringIO(f"{DET_PASSWORD}\n"), hide=True)
    done = []
    if user.username in parse_det_usernames(conn.run("det user list", hide=True).stdout):
        done.append("user exists")
    else:
        # The det CLI takes the password as an argument (visible in `ps` on the login node while it runs).
        conn.run(f"det user create {name} --password {q(user.password)}", hide=True)
        done.append("user created")
    conn.run(f"det user link-with-agent-user {name} --agent-uid {uid} --agent-user {name} "
             f"--agent-gid {gid} --agent-group {name}", hide=True)
    done.append("linked to agent user")
    if user.full_name:
        conn.run(f"det user edit {name} --display-name {q(user.full_name)}", hide=True)
        done.append("display name set")
    return ", ".join(done)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def describe_error(exc):
    """A one-line error message that never repeats the remote command line
    (it can contain a password or a password hash)."""
    result = getattr(exc, "result", None)
    if result is not None and hasattr(result, "exited") and type(exc).__name__ != "AuthFailure":
        output = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        tail = " | ".join(output.splitlines()[-3:])
        return f"remote command exited with {result.exited}" + (f": {tail}" if tail else "")
    return str(exc) or type(exc).__name__


def create_user(user, login_conn, docker_group=True, connect=None, log=print):
    """Run every step for one user. Returns a list of failure messages."""
    name = user.username
    failures = []

    def run_step(step, func, *args):
        try:
            result = func(*args)
        except Exception as e:
            message = describe_error(e)
            log(f"[{name}] {step}: FAILED: {message}")
            failures.append(f"{name}: {step}: {message}")
            raise
        status = result[-1] if isinstance(result, tuple) else result
        log(f"[{name}] {step}: {status}")
        return result

    # Linux account and TrueNAS objects: each step needs the previous one.
    try:
        uid, gid, _ = run_step("Linux user", ensure_linux_user, login_conn, user, docker_group)
        group_pk, _ = run_step("TrueNAS group", ensure_truenas_group, name, gid)
        run_step("TrueNAS user", ensure_truenas_user, name, uid, group_pk)
        run_step("TrueNAS home dataset", ensure_home_dataset, name)
        run_step("TrueNAS home ACL", ensure_home_acl, name, uid, gid)
        run_step("TrueNAS NFS share", ensure_nfs_share, name)
    except Exception:
        log(f"[{name}] skipping the remaining steps for this user")
        return failures

    for host, message in mount_home_all(name, login_conn, connect=connect, log=log):
        failures.append(f"{name}: mount on {host}: {message}")

    # The remaining steps do not depend on each other's success.
    for step, func, args in [
        ("home contents", ensure_home_contents, (login_conn, name)),
        ("Determined user", ensure_det_user, (login_conn, user, uid, gid)),
    ]:
        try:
            run_step(step, func, *args)
        except Exception:
            pass  # already recorded in failures
    try:
        run_step("Harbor user", ensure_harbor_user, name, user.password)
        run_step("Harbor project member", ensure_harbor_member, name)
    except Exception:
        pass  # already recorded in failures
    return failures


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Create new cluster users: login-node account, TrueNAS home + NFS share, "
                    "NFS mounts on all nodes, Determined AI and Harbor accounts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
users CSV (keep it out of git; scripts/new_users*.csv is gitignored):
  header: username,full_name,password   (see new_users.example.csv)
  - one user per row; fields that contain a comma or a double quote must be
    quoted ("..."), with "" for a literal double quote inside quotes
  - lines starting with '#' and blank lines are ignored
  - username: ^[a-z_][a-z0-9_-]{0,31}$
  - password: 8-128 characters, with an uppercase letter, a lowercase letter
    and a number; no control characters, no leading/trailing whitespace.
    The same password is set for Linux, Determined and Harbor (accounts that
    already exist keep their current password).

The whole file is validated before anything is changed. Every step skips what
already exists, so after a failure fix the cause and rerun the same command.
Exit status: 0 = all users done, 1 = some step failed, 2 = invalid input.

Admin credentials are read from my_secrets.py (TRUENAS_USERNAME,
TRUENAS_PASSWORD, SUDO_PASSWORD, DET_PASSWORD, HARBOR_PASSWORD); an SSH key
passphrase can be given in $SSH_PASSPHRASE.""")
    parser.add_argument("--users", required=True, metavar="CSV",
                        help="CSV file with the new users (username,full_name,password)")
    parser.add_argument("--no-docker-group", action="store_true",
                        help="do not add the new users to the docker group on the login node "
                             "(default: add them)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    users, errors = load_users(args.users)
    if errors:
        print("Invalid input, nothing was changed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 2

    failures = []
    failed_users = set()
    login_conn = make_connection(LOGIN_HOST)
    try:
        for user in users:
            user_failures = create_user(user, login_conn, docker_group=not args.no_docker_group)
            if user_failures:
                failed_users.add(user.username)
                failures.extend(user_failures)
            else:
                print(f"User {user.username} created successfully.")
    finally:
        login_conn.close()

    if failures:
        print(f"\n{len(failed_users)} of {len(users)} user(s) have failed steps:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("Fix the causes and rerun the same command; finished steps are skipped.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
