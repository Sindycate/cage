#!/usr/bin/python3
"""Remap one container account to the launching host's numeric identity."""

from __future__ import annotations

import grp
import os
import pwd
import re
import subprocess
import sys


MAX_ID = 4_294_967_294
TEMP_ID_MIN = 50_000
TEMP_ID_MAX = 59_999
GROUPMOD = "/usr/sbin/groupmod"
USERMOD = "/usr/sbin/usermod"


class RemapError(RuntimeError):
    """A requested identity cannot be applied safely."""


def parse_id(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise RemapError(f"{name} must be a decimal integer")
    value = int(raw, 10)
    if value == 0:
        raise RemapError(f"{name}=0 is not allowed")
    if value > MAX_ID:
        raise RemapError(f"{name} is outside the supported Linux ID range")
    return value


def run(*arguments: str) -> None:
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        operation = f"{os.path.basename(arguments[0])} {arguments[1]}"
        raise RemapError(f"{operation} failed{suffix}")


def unused_id(kind: str, reserved: set[int]) -> int:
    lookup = pwd.getpwuid if kind == "user" else grp.getgrgid
    for candidate in range(TEMP_ID_MAX, TEMP_ID_MIN - 1, -1):
        if candidate in reserved:
            continue
        try:
            lookup(candidate)
        except KeyError:
            return candidate
    raise RemapError(f"no temporary {kind} ID is available")


def remap_group(target_user: str, desired_gid: int) -> None:
    target = pwd.getpwnam(target_user)
    current_gid = target.pw_gid
    try:
        target_group = grp.getgrgid(current_gid)
    except KeyError as exc:
        raise RemapError(
            f"target user {target_user!r} has no resolvable primary group"
        ) from exc
    if current_gid == desired_gid:
        return

    try:
        occupant = grp.getgrgid(desired_gid)
    except KeyError:
        occupant = None
    if occupant is None:
        run(GROUPMOD, "-g", str(desired_gid), target_group.gr_name)
        return
    if occupant.gr_name == target_group.gr_name:
        raise RemapError(
            f"target group {target_group.gr_name!r} resolved to inconsistent GIDs"
        )

    temporary_gid = unused_id("group", {current_gid, desired_gid})
    run(GROUPMOD, "-g", str(temporary_gid), target_group.gr_name)
    run(GROUPMOD, "-g", str(current_gid), occupant.gr_name)
    run(GROUPMOD, "-g", str(desired_gid), target_group.gr_name)


def remap_user(target_user: str, desired_uid: int) -> None:
    target = pwd.getpwnam(target_user)
    current_uid = target.pw_uid
    if current_uid == desired_uid:
        return

    try:
        occupant = pwd.getpwuid(desired_uid)
    except KeyError:
        occupant = None
    if occupant is None:
        run(USERMOD, "-u", str(desired_uid), target_user)
        return
    if occupant.pw_name == target_user:
        raise RemapError(f"target user {target_user!r} resolved to inconsistent UIDs")

    temporary_uid = unused_id("user", {current_uid, desired_uid})
    run(USERMOD, "-u", str(temporary_uid), target_user)
    run(USERMOD, "-u", str(current_uid), occupant.pw_name)
    run(USERMOD, "-u", str(desired_uid), target_user)


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z_][a-z0-9_-]*", sys.argv[1]):
        raise RemapError("expected one safe target account name")
    target_user = sys.argv[1]
    desired_uid = parse_id("HOST_UID")
    desired_gid = parse_id("HOST_GID")
    if desired_uid is None and desired_gid is None:
        return 0
    if desired_uid is None or desired_gid is None:
        raise RemapError("HOST_UID and HOST_GID must be provided together")
    try:
        pwd.getpwnam(target_user)
    except KeyError as exc:
        raise RemapError(f"target account {target_user!r} does not exist") from exc

    remap_group(target_user, desired_gid)
    remap_user(target_user, desired_uid)
    final = pwd.getpwnam(target_user)
    if (final.pw_uid, final.pw_gid) != (desired_uid, desired_gid):
        raise RemapError(
            f"target account {target_user!r} remained {final.pw_uid}:{final.pw_gid} "
            f"instead of {desired_uid}:{desired_gid}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RemapError as exc:
        sys.stderr.write(f"cage: refusing unsafe container user remap: {exc}\n")
        raise SystemExit(1)
