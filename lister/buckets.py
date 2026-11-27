from acl import PERMS, BadPath, normalise
from place import HOME, home_owner

READING = ("read", "list")
USERS = "users/"


def bucket_id_of(prefix, user):
    owner = home_owner(prefix)
    if owner is not None:
        return "home" if owner == user else f"home-{owner}"
    if prefix.startswith(USERS):
        return None
    return "shared"


def canonical(path):
    try:
        return normalise(path) == path
    except BadPath:
        return False


def named(user):
    return bool(user) and HOME.fullmatch(f"users/{user}/") is not None


def held(acl, user, groups):
    shared, homes = set(), {}
    for perm in PERMS:
        for path in acl.readable_prefixes(user, groups, perm):
            if canonical(path) and not path.startswith(USERS):
                shared.add(perm)
        if not named(user):
            continue
        for path in acl.readable_prefixes(user, [], perm):
            owner = home_owner(path)
            if owner is not None and canonical(path):
                homes.setdefault(owner, set()).add(perm)
    return shared, homes


def ordered(perms):
    return [p for p in PERMS if p in perms]


def readable(perms):
    return any(p in perms for p in READING)


def buckets_for(acl, user, groups):
    shared, homes = held(acl, user, groups)
    out = []
    if user in homes and readable(homes[user]):
        out.append({"id": "home", "label": "Your files", "root_url": f"/users/{user}/",
                    "access": ordered(homes[user]), "kind": "home"})
    if readable(shared):
        out.append({"id": "shared", "label": "Shared", "root_url": "/",
                    "access": ordered(shared), "kind": "shared"})
    for owner in sorted(homes):
        if owner != user and readable(homes[owner]):
            out.append({"id": f"home-{owner}", "label": f"{owner}'s files", "root_url": f"/users/{owner}/",
                        "access": ordered(homes[owner]), "kind": "granted"})
    return out
