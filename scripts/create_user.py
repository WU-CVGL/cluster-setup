"""Create new users on the cluster."""

import requests
from io import StringIO
from os import getenv

from fabric import Connection, Config
from my_secrets import TRUENAS_USERNAME, TRUENAS_PASSWORD, SUDO_PASSWORD, DET_PASSWORD, HARBOR_PASSWORD


TRUENAS_API_URL = "http://10.0.1.70/api/v2.0"
HARBOR_API_URL = "http://10.0.1.68:50000/api/v2.0"

connect_kwargs = {
    'passphrase': getenv('SSH_PASSPHRASE')  # SSH private key passphrase
}

def create_user_login_node(username, password):
    # Connect to the server
    config = Config(overrides={'sudo': {'password': SUDO_PASSWORD}})
    conn = Connection("cvgladmin@login", config=config, connect_kwargs=connect_kwargs)

    # Create the user
    conn.sudo(f"useradd -m -s /bin/bash {username}")
    conn.sudo(f"bash -c 'echo {username}:{password} | chpasswd'")

    # Check if the user was created, and get the user ID and group ID
    uid = conn.run(f"id -u {username}").stdout.strip()
    gid = conn.run(f"id -g {username}").stdout.strip()
    return uid, gid


def create_group_truenas(username, gid):
    data = {
        "gid": gid,
        "name": username,
        "smb": False
    }
    response = requests.post(
        f"{TRUENAS_API_URL}/group/",
        auth=(TRUENAS_USERNAME, TRUENAS_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
    )
    response.raise_for_status()
    return response.json()


def check_group_id_truenas(username):
    response = requests.get(
        f"{TRUENAS_API_URL}/group/?name={username}",
        auth=(TRUENAS_USERNAME, TRUENAS_PASSWORD),
    )
    response.raise_for_status()
    return response.json()[0]["id"]


def create_user_truenas(username, uid):
    group_id_truenas = check_group_id_truenas(username)
    assert group_id_truenas is not None
    data = {
        "password_disabled": True,
        "group_create": False,
        "username": username,
        "full_name": username,
        "uid": uid,
        "group": group_id_truenas,
        "smb": False
    }
    response = requests.post(
        f"{TRUENAS_API_URL}/user/",
        auth=(TRUENAS_USERNAME, TRUENAS_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
    )
    response.raise_for_status()
    return response.json()


def create_home_truenas(username):
    data = {
        "name": f"Peter/Workspace/{username}",
        "quota": 8 * 1024**4  # 8TB
    }
    response = requests.post(
        f"{TRUENAS_API_URL}/pool/dataset/",
        auth=(TRUENAS_USERNAME, TRUENAS_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
    )
    response.raise_for_status()
    return response.json()


def update_home_acl_truenas(username):
    # Reference: 
    # https://github.com/truenas/middleware/blob/master/src/middlewared/middlewared/plugins/pool_/dataset_quota_and_perms.py
    # https://github.com/truenas/middleware/blob/master/tests/api2/test_345_acl_nfs4.py
    data = {
        "user": username,
        "group": username,
        "acl": [
            {
                "flags": {"BASIC": "INHERIT"},
                "id": None,
                "perms": {"BASIC": "FULL_CONTROL"},
                "tag": "owner@",
                "type": "ALLOW"
            }
        ],
        "options": {
            "set_default_acl": True,
            "stripacl": False,
            "recursive": True,
            "traverse": True
        }
    }
    response = requests.post(
        f"{TRUENAS_API_URL}/pool/dataset/id/Peter%2FWorkspace%2F{username}/permission",
        auth=(TRUENAS_USERNAME, TRUENAS_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
    )
    response.raise_for_status()
    return response.json()


def create_home_nfs_share_truenas(username):
    data = {
        "path": f"/mnt/Peter/Workspace/{username}",
        "networks": ["192.168.233.0/24", "10.0.1.64/27"],
        "enabled": True
    }
    response = requests.post(
        f"{TRUENAS_API_URL}/sharing/nfs",
        auth=(TRUENAS_USERNAME, TRUENAS_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
    )
    response.raise_for_status()
    return response.json()


def mount_home_all(username):
    # Connect to the server
    config = Config(overrides={'sudo': {'password': SUDO_PASSWORD}})
    for server in ["cvgladmin@login","S1", "S2", "S3", "S4", "S5", "S6","S7", "S8"]:
        conn = Connection(server, config=config, connect_kwargs=connect_kwargs)

        # Mount the NFS share
        mount_sources = [f"nas.cvgl.lab:/mnt/Peter/Workspace/{username}"]
        mount_targets = [f"/workspace/{username}"]

        if "login" in server:
            mount_sources.append(mount_sources[0])
            mount_targets.append(f"/home/{username}")

        for mount_source, mount_target in zip(mount_sources, mount_targets):
            conn.sudo(f"mkdir -p {mount_target}")

            # append the following line to /etc/fstab
            mount_string = f"{mount_source} {mount_target} nfs defaults,vers=3,async,noatime,soft,rsize=32769,wsize=32768,_netdev 0 2"
            conn.sudo(f"bash -c 'echo {mount_string} >> /etc/fstab'")
            result = conn.sudo("mount -a").stdout
            assert result == "", f"Failed to mount the NFS share: {result}"

def generate_home_contents_login_node(username, password):
    # Connect to the server
    config = Config(overrides={'sudo': {'password': SUDO_PASSWORD}})
    conn = Connection("cvgladmin@login", config=config, connect_kwargs=connect_kwargs)

    # Update the user's home directory
    conn.sudo("xdg-user-dirs-update --force", user=username)
    try:
        conn.sudo(f"cp /etc/skel/.* /home/{username}", user=username, hide='stderr')
    except:
        # ignore the unimportant errors
        pass


def create_user_det(username, password, uid, gid, fullname=None):
    # Connect to the server
    conn = Connection("cvgladmin@login")

    # Login to the Determined CLI
    conn.run("det user login admin", in_stream=StringIO(f"{DET_PASSWORD}\n"))
    conn.run(f"det user create {username} --password {password}")
    conn.run(f"det user link-with-agent-user {username} --agent-uid {uid} --agent-user {username} --agent-gid {gid} --agent-group {username}")
    if fullname:
        conn.run(f"det user edit {username} --display-name {fullname}")
    conn.run("det user list")


def create_user_harbor(username, password):
    data = {
        "username": username,
        "password": password,
        "realname": username,
        "email": f"{username}@example.com"
    }
    response = requests.post(
        f"{HARBOR_API_URL}/users",
        auth=("admin", HARBOR_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
    )
    response.raise_for_status()


def add_user_to_harbor_project_member(username):
    data = {
        "role_id": 2,  # 2 is the ID for the "Maintainer" role
        "member_user": {
            "username": username
        }
    }
    response = requests.post(
        f"{HARBOR_API_URL}/projects/1/members",
        auth=("admin", HARBOR_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
    )
    response.raise_for_status()


def create_user(username, password, fullname=None):
    uid, gid = create_user_login_node(username, password)
    create_group_truenas(username, gid)
    create_user_truenas(username, uid)
    create_home_truenas(username)
    update_home_acl_truenas(username)
    create_home_nfs_share_truenas(username)
    mount_home_all(username)
    generate_home_contents_login_node(username, password)
    create_user_det(username, password, uid, gid, fullname)
    create_user_harbor(username, password)
    add_user_to_harbor_project_member(username)

usernames = [
    # fill in the usernames
]

full_names = [
    # fill in the full names
]

passwords = [
    # fill in the passwords
]

for username, password, fullname in zip(usernames, passwords, full_names):
    create_user(username, password, fullname)
    print(f"User {username} created successfully.")
