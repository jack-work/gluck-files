#!/usr/bin/env python3
"""Per-path access control for the files bucket.

Garage cannot express a prefix grant: `garage bucket allow` takes --read,
--write, --owner and a bucket, and nothing narrower. So the bucket is the
smallest unit it has, and anything finer has to be decided here, in front of it.

The model is deliberately small:

    a grant is (subject, path, perm)
    a grant on a PREFIX covers everything beneath it
    nothing is permitted that was not granted

`subject` is either `user:<name>` or `group:<name>`, so a grant can follow an
lldap group without this table learning about the directory.
"""

import os
import sqlite3
import threading
import time

PERMS = ("read", "list", "write")

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
  subject    TEXT NOT NULL,
  path       TEXT NOT NULL,
  perm       TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  granted_at INTEGER NOT NULL,
  PRIMARY KEY (subject, path, perm)
);
CREATE INDEX IF NOT EXISTS grants_subject ON grants (subject);
"""


class BadPath(ValueError):
    """A path that cannot be trusted, rather than one that is merely denied."""


def normalise(path):
    """Canonical form: no leading slash, no '.' or '..', collapsed separators.

    A directory keeps its trailing slash, because `dad/` and `dad` mean
    different things to a prefix match and the distinction is load-bearing:
    a grant on `dad/` must not also cover a sibling object called `dadtaxes`.
    """
    if path is None:
        raise BadPath("no path")
    if "\x00" in path:
        raise BadPath("null byte")
    trailing = path.endswith("/")
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            # Refuse rather than resolve. A request that needs to climb is a
            # request someone built by hand.
            raise BadPath("parent traversal")
        parts.append(part)
    out = "/".join(parts)
    if out and trailing:
        out += "/"
    return out


def ancestors(path):
    """`a/b/c.pdf` -> ['', 'a/', 'a/b/', 'a/b/c.pdf'] — every prefix that could
    carry an inherited grant, root first."""
    path = normalise(path)
    out = [""]
    parts = [p for p in path.split("/") if p]
    walked = ""
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        walked += part + ("" if last and not path.endswith("/") else "/")
        out.append(walked)
    if path and out[-1] != path:
        out.append(path)
    return out


def subjects_for(user, groups):
    """Everything a caller can match a grant on."""
    out = []
    if user:
        out.append(f"user:{user}")
    for g in groups or []:
        if g:
            out.append(f"group:{g}")
    return out


class Acl:
    def __init__(self, path):
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        first = not os.path.exists(path)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._fresh = first

    def is_fresh(self):
        """True if this process created the database. Used once, to seed the
        admin grant, so that admin is a row rather than a special case."""
        return self._fresh

    # ── writes, from the admin CLI only ──────────────────────────────────
    def grant(self, subject, path, perm, granted_by, now=None):
        if perm not in PERMS:
            raise ValueError(f"perm must be one of {PERMS}")
        if not subject.startswith(("user:", "group:")):
            raise ValueError("subject must be user:<name> or group:<name>")
        path = normalise(path)
        now = int(now if now is not None else time.time())
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO grants "
                "(subject, path, perm, granted_by, granted_at) VALUES (?,?,?,?,?)",
                (subject, path, perm, granted_by, now),
            )
        return {"subject": subject, "path": path, "perm": perm}

    def revoke(self, subject, path, perm=None):
        path = normalise(path)
        with self._lock:
            if perm is None:
                cur = self._db.execute(
                    "DELETE FROM grants WHERE subject=? AND path=?", (subject, path)
                )
            else:
                cur = self._db.execute(
                    "DELETE FROM grants WHERE subject=? AND path=? AND perm=?",
                    (subject, path, perm),
                )
        return cur.rowcount

    def revoke_subject(self, subject):
        with self._lock:
            cur = self._db.execute("DELETE FROM grants WHERE subject=?", (subject,))
        return cur.rowcount

    # ── reads ────────────────────────────────────────────────────────────
    def list_grants(self, subject=None):
        q = "SELECT subject, path, perm, granted_by, granted_at FROM grants"
        args = ()
        if subject:
            q += " WHERE subject=?"
            args = (subject,)
        q += " ORDER BY subject, path, perm"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [
            {"subject": s, "path": p, "perm": m, "granted_by": b, "granted_at": t}
            for (s, p, m, b, t) in rows
        ]

    def allows(self, user, groups, path, perm):
        """True if any grant on this path or an ancestor permits it.

        Deny is the absence of a grant, not a record. There is no negative
        grant, because a deny entry that silently loses a precedence race is
        worse than not having one.
        """
        if perm not in PERMS:
            raise ValueError(f"perm must be one of {PERMS}")
        subs = subjects_for(user, groups)
        if not subs:
            return False
        try:
            prefixes = ancestors(path)
        except BadPath:
            return False
        qs = ",".join("?" * len(subs))
        ps = ",".join("?" * len(prefixes))
        with self._lock:
            row = self._db.execute(
                f"SELECT 1 FROM grants WHERE perm=? AND subject IN ({qs}) "
                f"AND path IN ({ps}) LIMIT 1",
                (perm, *subs, *prefixes),
            ).fetchone()
        return row is not None

    def readable_prefixes(self, user, groups, perm="read"):
        """Every granted path for this caller, for filtering a listing."""
        subs = subjects_for(user, groups)
        if not subs:
            return []
        qs = ",".join("?" * len(subs))
        with self._lock:
            rows = self._db.execute(
                f"SELECT path FROM grants WHERE perm=? AND subject IN ({qs})",
                (perm, *subs),
            ).fetchall()
        return sorted({r[0] for r in rows})

    def close(self):
        with self._lock:
            self._db.close()
