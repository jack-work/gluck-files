import re

from acl import PERMS, BadPath, normalise
from place import HOME, home_owner

USERS = "users/"

RESERVED = re.compile(r"(user-)*(home|shared)")


def granted_id(owner):
    return f"user-{owner}" if RESERVED.fullmatch(owner) else owner


def current_id(prefix, buckets):
    here = "/" + prefix
    best = None
    for b in buckets:
        if here.startswith(b["root_url"]) and (best is None or len(b["root_url"]) > len(best["root_url"])):
            best = b
    return best["id"] if best else None


def default_bucket(buckets):
    for kind in ("home", "shared"):
        for b in buckets:
            if b["kind"] == kind:
                return b
    return buckets[0] if buckets else None


def canonical(path):
    try:
        return normalise(path) == path
    except BadPath:
        return False


def named(user):
    return bool(user) and HOME.fullmatch(f"users/{user}/") is not None


def held(acl, user, groups):
    shared, root, homes = set(), set(), {}
    for perm in PERMS:
        for path in acl.readable_prefixes(user, groups, perm):
            if canonical(path) and not path.startswith(USERS):
                shared.add(perm)
                if path == "":
                    root.add(perm)
        if not named(user):
            continue
        for path in acl.readable_prefixes(user, [], perm):
            owner = home_owner(path)
            if owner is not None and canonical(path):
                homes.setdefault(owner, set()).add(perm)
    return shared, root, homes


def ordered(perms):
    return [p for p in PERMS if p in perms]


def readable(perms):
    return "list" in perms


def buckets_for(acl, user, groups):
    shared, root, homes = held(acl, user, groups)
    out = []
    rooted = False
    if user in homes and readable(homes[user]):
        out.append({"id": "home", "label": "Your files", "root_url": f"/users/{user}/",
                    "access": ordered(homes[user]), "kind": "home"})
    elif readable(root):
        rooted = True
        out.append({"id": "home", "label": "Your files", "root_url": "/",
                    "access": ordered(root), "kind": "home"})
    granted = [{"id": granted_id(owner), "label": owner, "root_url": f"/users/{owner}/",
                "access": ordered(homes[owner]), "kind": "granted"}
               for owner in homes if owner != user and readable(homes[owner])]
    out.extend(sorted(granted, key=lambda b: b["label"]))
    if readable(shared) and not rooted:
        out.append({"id": "shared", "label": "Shared", "root_url": "/",
                    "access": ordered(shared), "kind": "shared"})
    return out
