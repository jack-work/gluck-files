#!/usr/bin/env python3
"""Admin control of the file-store ACLs. Runs ON spain, as root, over ssh.

Authority here is ssh access to spain, which is already the strongest credential
on the estate. That is deliberate: the alternative was an authenticated admin
API on a public hostname, which would mean a new authz surface, a bearer path in
front of a service that does not verify JWTs, and a second way to grant access.
There is no web portal and no self-service; the admin brokers every share.

  gluck-files-acl grant  <subject> <path> [--read] [--list] [--write]
  gluck-files-acl revoke <subject> <path> [--read] [--list] [--write]
  gluck-files-acl drop   <subject>
  gluck-files-acl ls     [subject]
  gluck-files-acl check  <user> <path> <perm> [--groups a,b]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acl import PERMS, Acl, BadPath

DEFAULT_DB = os.environ.get("LISTER_ACL_DB", "/var/lib/gluck-files-acl/acl.db")


def subject(raw):
    """Accept `dad` as shorthand for `user:dad`, because that is what a human
    types, but store the qualified form so a group grant is never ambiguous."""
    if raw.startswith(("user:", "group:")):
        return raw
    return f"user:{raw}"


def perms_from(args):
    chosen = [p for p in PERMS if getattr(args, p)]
    return chosen or None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gluck-files-acl")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def perm_flags(p):
        for name in PERMS:
            p.add_argument(f"--{name}", action="store_true")

    g = sub.add_parser("grant", help="grant a permission on a path and below")
    g.add_argument("subject")
    g.add_argument("path")
    perm_flags(g)

    r = sub.add_parser("revoke", help="remove a permission on exactly this path")
    r.add_argument("subject")
    r.add_argument("path")
    perm_flags(r)

    d = sub.add_parser("drop", help="remove every grant a subject holds")
    d.add_argument("subject")

    l = sub.add_parser("ls", help="show grants")
    l.add_argument("subject", nargs="?")

    c = sub.add_parser("check", help="ask the same question the gateway asks")
    c.add_argument("user")
    c.add_argument("path")
    c.add_argument("perm", choices=PERMS)
    c.add_argument("--groups", default="")

    args = ap.parse_args(argv)
    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    acl = Acl(args.db)

    def out(obj, text):
        print(json.dumps(obj) if args.json else text)

    try:
        if args.cmd == "grant":
            chosen = perms_from(args)
            if not chosen:
                ap.error("grant needs at least one of --read --list --write")
            done = [acl.grant(subject(args.subject), args.path, p, "admin") for p in chosen]
            out(done, "\n".join(
                f"granted {d['perm']:5} on {d['path'] or '/'} to {d['subject']}" for d in done))

        elif args.cmd == "revoke":
            chosen = perms_from(args)
            n = 0
            if chosen:
                for p in chosen:
                    n += acl.revoke(subject(args.subject), args.path, p)
            else:
                n = acl.revoke(subject(args.subject), args.path)
            out({"removed": n}, f"removed {n} grant(s)")

        elif args.cmd == "drop":
            n = acl.revoke_subject(subject(args.subject))
            out({"removed": n}, f"removed {n} grant(s) from {subject(args.subject)}")

        elif args.cmd == "ls":
            rows = acl.list_grants(subject(args.subject) if args.subject else None)
            if args.json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print("no grants")
            else:
                print(f"{'SUBJECT':24} {'PERM':6} PATH")
                for row in rows:
                    print(f"{row['subject']:24} {row['perm']:6} {row['path'] or '/'}")

        elif args.cmd == "check":
            groups = [g for g in args.groups.split(",") if g]
            ok = acl.allows(args.user, groups, args.path, args.perm)
            out({"allowed": ok}, f"{'ALLOW' if ok else 'DENY '} {args.user} {args.perm} {args.path}")
            return 0 if ok else 1
    except (BadPath, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    finally:
        acl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
