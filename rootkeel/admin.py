# SPDX-License-Identifier: AGPL-3.0-only
"""Operator session administration: list machines and grants, end grants, disable machines.

Grants have no end by default, so ending access is an administrative act. Run in
the broker's container, next to its database (it needs no owner credential):

    python3 -m rootkeel.admin machines
    python3 -m rootkeel.admin list
    python3 -m rootkeel.admin revoke (--grant ID | --all [--session NAME] | --machine ID)
    python3 -m rootkeel.admin machine-revoke (--machine ID | --all)

The running broker closes the ended grants' open sockets within half a second
(Broker.sweep) and then revokes their Zellij tokens. Ending access never stops
the machine's Zellij session or its processes; `zellij kill-session NAME` on the
machine does.
"""
import argparse
import os
import sys
import time

from . import config
from .store import Store


def grants(store, args):
    """Open grants matching the selection."""
    if args.grant:
        rows = store.all("SELECT g.id FROM grants g WHERE g.id=? AND g.revoked IS NULL", (args.grant,))
    elif args.machine:
        rows = store.all("SELECT g.id FROM grants g WHERE g.machine=? AND g.revoked IS NULL", (args.machine,))
    else:
        query, params = "SELECT g.id FROM grants g WHERE g.revoked IS NULL", ()
        if getattr(args, "session", None):
            query, params = query + " AND g.session=?", (args.session,)
        rows = store.all(query + " ORDER BY g.issued", params)
    return [store.grant(row["id"]) for row in rows]


def machine_ids(store, value):
    """The enabled machine named by id or name."""
    return [row["id"] for row in store.db.execute("SELECT id FROM machines WHERE enabled=1 AND (id=? OR name=?)",
                                                  (value, value)).fetchall()]


def when(stamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "-"


def table(rows):
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m rootkeel.admin", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("machines", help="enrolled machines")
    sub.add_parser("list", help="open session grants")
    revoke = sub.add_parser("revoke", help="end session grants now; their open connections close at once")
    which = revoke.add_mutually_exclusive_group(required=True)
    which.add_argument("--grant", help="one grant id (g_...)")
    which.add_argument("--all", action="store_true", help="every open grant (with --session: only that session's)")
    which.add_argument("--machine", help="every open grant on this machine (id or name)")
    revoke.add_argument("--session", help="with --all: only grants for this session name")
    machine = sub.add_parser("machine-revoke", help="disable machines and end all their grants")
    which = machine.add_mutually_exclusive_group(required=True)
    which.add_argument("--machine", help="one machine (id m_... or name)")
    which.add_argument("--all", action="store_true", help="every enabled machine")
    args = parser.parse_args(argv)
    if args.command == "revoke" and args.session and not args.all:
        parser.error("--session needs --all")
    try:
        store = Store(os.environ.get("RT_DB", "/data/broker.sqlite"), limits=config.grant_limits(os.environ))
    except (ValueError, OSError) as error:
        print(f"Cannot open the broker database: {error}", file=sys.stderr)
        return 1
    try:
        if args.command == "machines":
            rows = [("MACHINE", "NAME", "STATE", "ENROLLED")]
            for row in store.db.execute("SELECT id, name, enabled, enrolled FROM machines ORDER BY enabled DESC, enrolled"):
                rows.append((row["id"], row["name"] or "-", "enabled" if row["enabled"] else "revoked", when(row["enrolled"])))
            table(rows)
            return 0
        if args.command == "list":
            args.grant = args.machine = None
            rows = [("GRANT", "MACHINE", "SESSION", "ISSUED", "LAST INPUT", "ENDS")]
            for grant in grants(store, args):
                ends = store.ends(grant)
                rows.append((grant["id"], grant["machine_name"] or grant["machine"], grant["session"], when(grant["issued"]),
                             when(grant["last_input"]), when(ends) if ends else "never (revoke ends it)"))
            table(rows)
            return 0
        if args.machine:
            found = machine_ids(store, args.machine)
            if args.command == "revoke" and not found:
                # A revoked machine's grants are ended already; an unknown one has none.
                found = [args.machine]
            args.machine = found[0] if found else None
        if args.command == "machine-revoke":
            if args.all:
                machines = [row["id"] for row in store.db.execute("SELECT id FROM machines WHERE enabled=1")]
            else:
                machines = [args.machine] if args.machine else []
            for mid in machines:
                store.execute("UPDATE machines SET enabled=0 WHERE id=?", (mid,))
                store.audit("machine-revoked", actor="operator", machine=mid)
            args.grant = None
            args.session = None
            ended = []
            for mid in machines:
                args.machine = mid
                ended += [(g, "machine-revoked") for g in grants(store, args)]
            print(f"Disabled {len(machines)} machine(s): " + (", ".join(machines) or "none matched"))
        else:
            ended = [(g, "operator-revoked") for g in grants(store, args)]
        for grant, reason in ended:
            store.execute("UPDATE grants SET revoked=? WHERE id=? AND revoked IS NULL", (reason, grant["id"]))
            store.audit(reason, actor="operator", machine=grant["machine"], session=grant["session"], grant_id=grant["id"])
        print(f"Ended {len(ended)} session grant(s)" + (": " + ", ".join(g["id"] for g, _ in ended) if ended else "") +
              ". The broker closes their open connections within a second; the machine's sessions keep running.")
        return 0
    finally:
        store.db.close()


if __name__ == "__main__":
    sys.exit(main())
