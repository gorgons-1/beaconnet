"""Administration console (bbs-admin). Every action is authorised and audited by bbs-core."""

from __future__ import annotations

import json

from bbs_gateway.client import ApiError, Session
from bbs_gateway.sshserver import BBSServer, Gateway
from bbs_gateway.term import WIDTH, Term, c, short_time, wrap


async def _err(t: Term, exc: ApiError) -> None:
    t.error(exc.message)


def _user_line(u: dict) -> str:
    call = c(u.get("callsign") or "-") + ("" if u.get("callsign_verified") or not u.get("callsign") else "?")
    return f"{u['id']:>5}. {c(u['username'])[:20]:<20} {call:<10} {c(u['role']):<5} {c(u['status']):<9} {short_time(u['created_at'])}"


async def user_detail(t: Term, s: Session, uid: int) -> None:
    while True:
        try:
            u = await s.get(f"/v1/admin/users/{uid}")
        except ApiError as exc:
            return await _err(t, exc)
        t.title(f"User {u['id']}: {c(u['username'])}")
        t.line(f"Callsign:  {c(u.get('callsign') or '-')}  " + ("(verified)" if u["callsign_verified"] else "(UNVERIFIED claim)" if u.get("callsign") else ""))
        t.line(f"Display:   {c(u.get('display_name') or '-')}")
        t.line(f"Contact:   {c(u.get('contact') or '-')}   (private)")
        t.line(f"Role:      {c(u['role'])}   Status: {c(u['status'])}" + (f" - {c(u['status_reason'])}" if u.get("status_reason") else ""))
        t.line(f"Created:   {c(u['created_at'])}   Approved: {c(u.get('approved_at') or '-')}")
        t.line("Keys:")
        for k in u["keys"]:
            state = f"REVOKED {short_time(k['revoked_at'])}" if k["revoked_at"] else f"used {short_time(k['last_used_at'])}"
            t.line(f"  {k['id']:>4}. {c(k['type'])[:11]:<11} {c(k['fingerprint'])[:52]} {state}")
        if u["registrations"]:
            t.line("Registration history:")
            for r in u["registrations"]:
                t.line(f"  {short_time(r['requested_at'])} from {c(r.get('client_ip') or '-')} via {c(r['transport'])}: "
                       f"{c(r.get('decision') or 'PENDING')}" + (f" by {c(r['decided_by'])}" if r.get("decided_by") else "")
                       + (f" ({c(r['reason'])})" if r.get("reason") else ""))
        opts = []
        st = u["status"]
        if st == "PENDING":
            opts += ["A=approve", "R=reject"]
        if st in ("ACTIVE", "PENDING"):
            opts.append("S=suspend")
        if st == "SUSPENDED":
            opts.append("U=reactivate")
        if u.get("callsign"):
            opts.append("V=toggle callsign verified")
        opts += ["K=revoke a key", "Q=back"]
        ans = (await t.readline(", ".join(opts) + ": ", 5)).upper()
        try:
            if ans == "A" and st == "PENDING":
                verified = None
                if u.get("callsign"):
                    verified = await t.confirm(f"Have you verified that {c(u['callsign'])} belongs to this person?")
                await s.post(f"/v1/admin/users/{uid}/status", {"action": "approve", "callsign_verified": verified})
                t.line("Approved - the account is ACTIVE.")
            elif ans == "R" and st == "PENDING":
                reason = await t.ask("Reason (shown to the applicant): ", 200)
                await s.post(f"/v1/admin/users/{uid}/status", {"action": "reject", "reason": reason or None})
                t.line("Rejected.")
            elif ans == "S" and st in ("ACTIVE", "PENDING"):
                reason = await t.ask("Reason (shown to the user): ", 200)
                if await t.confirm("Suspend now? Their sessions end immediately."):
                    await s.post(f"/v1/admin/users/{uid}/status", {"action": "suspend", "reason": reason or None})
                    t.line("Suspended.")
            elif ans == "U" and st == "SUSPENDED":
                await s.post(f"/v1/admin/users/{uid}/status", {"action": "reactivate"})
                t.line("Reactivated.")
            elif ans == "V" and u.get("callsign"):
                await s.post(f"/v1/admin/users/{uid}/callsign", {"verified": not u["callsign_verified"]})
            elif ans == "K":
                kid = await t.ask("Key number to revoke: ", 6)
                if kid.isdigit() and await t.confirm(f"Revoke key {kid}? Sessions using it end immediately."):
                    await s.delete(f"/v1/admin/keys/{kid}")
                    t.line("Key revoked.")
            elif ans in ("Q", ""):
                return
        except ApiError as exc:
            await _err(t, exc)


async def user_list(t: Term, s: Session, status: str, heading: str) -> None:
    offset = 0
    while True:
        try:
            data = await s.get("/v1/admin/users", status=status, offset=offset, limit=15)
        except ApiError as exc:
            return await _err(t, exc)
        t.title(heading)
        if not data["users"]:
            t.line("None.")
        else:
            t.line(f"{'ID':>6} {'Username':<20} {'Callsign':<10} {'Role':<5} {'Status':<9} Created")
            for u in data["users"]:
                t.line(_user_line(u))
            t.line("(? after a callsign = not verified)")
        opts = ["ID=open"] + (["N=next"] if data["more"] else []) + (["P=prev"] if offset else []) + ["Q=back"]
        ans = (await t.readline(", ".join(opts) + ": ", 8)).upper()
        if ans.isdigit():
            await user_detail(t, s, int(ans))
        elif ans == "N" and data["more"]:
            offset += 15
        elif ans == "P" and offset:
            offset = max(0, offset - 15)
        elif ans in ("Q", ""):
            return


async def moderation(t: Term, s: Session) -> None:
    status = None
    while True:
        try:
            data = await s.get("/v1/admin/messages", status=status, limit=15)
        except ApiError as exc:
            return await _err(t, exc)
        t.title("Bulletin moderation" + (f" - {status}" if status else " - all"))
        for m in data["messages"]:
            flag = "" if m["status"] == "VISIBLE" else f"[{m['status']}] "
            t.line(f"{m['number']:>6} {short_time(m['created_at'])} {c(m['board'])[:10]:<10} {c(m['author'])[:10]:<10} "
                   f"{flag}{c(m['subject'])[:30]}")
        ans = (await t.readline("number=moderate, H=hidden only, A=all, B=manage boards, Q=back: ", 8)).upper()
        if ans.isdigit():
            await moderate_one(t, s, int(ans))
        elif ans == "H":
            status = "HIDDEN"
        elif ans == "A":
            status = None
        elif ans == "B":
            await boards_menu(t, s)
        elif ans in ("Q", ""):
            return


async def moderate_one(t: Term, s: Session, number: int) -> None:
    try:
        m = await s.get(f"/v1/messages/{number}")
    except ApiError as exc:
        return await _err(t, exc)
    lines = ["", f"#{m['number']} {c(m['board'])} from {c(m['author'])} ({c(m.get('callsign') or '-')}) {c(m['created_at'])}",
             f"Status: {c(m['status'])}" + (f" - {c(m['moderation_reason'])}" if m.get("moderation_reason") else ""),
             f"Subj: {c(m['subject'])}", "-" * (WIDTH - 1), *wrap(m["body"]), "-" * (WIDTH - 1)]
    await t.page(lines)
    ans = (await t.readline("H=hide, U=unhide, D=delete permanently, Enter=back: ", 5)).upper()
    action = {"H": "hide", "U": "restore", "D": "delete"}.get(ans)
    if not action:
        return
    reason = await t.ask("Reason (for the audit log): ", 200)
    if action == "delete" and not await t.confirm("Delete the content? This can't be undone."):
        return
    try:
        r = await s.post(f"/v1/admin/messages/{number}/moderate", {"action": action, "reason": reason or None})
        t.line(f"Message #{number} is now {c(r['status'])}.")
    except ApiError as exc:
        await _err(t, exc)


async def boards_menu(t: Term, s: Session) -> None:
    while True:
        try:
            boards = (await s.get("/v1/boards"))["boards"]
        except ApiError as exc:
            return await _err(t, exc)
        t.title("Boards")
        for b in boards:
            flags = ",".join(f for f, on in (("archived", b["archived"]), ("emergency", b["emergency"]),
                                             ("private", not b["public"])) if on)
            t.line(f"  {c(b['name']):<14} {b['count']:>5} msgs  {flags}")
        ans = (await t.readline("C=create, R=rename, A=archive, U=unarchive, D=describe, Q=back: ", 5)).upper()
        try:
            if ans == "C":
                name = (await t.ask("New board name: ", 24)).upper()
                desc = await t.ask("Description: ", 80)
                admins_only = await t.confirm("Only admins may post?")
                public = await t.confirm("Readable by guests?", default=True)
                await s.post("/v1/admin/boards", {"action": "create", "name": name, "description": desc,
                                                  "post": "admins" if admins_only else "users", "public": public})
                t.line("Created.")
            elif ans in ("R", "A", "U", "D"):
                name = (await t.ask("Board: ", 24)).upper()
                body: dict = {"name": name, "action": {"R": "rename", "A": "archive", "U": "unarchive", "D": "describe"}[ans]}
                if ans == "R":
                    body["new_name"] = (await t.ask("New name: ", 24)).upper()
                if ans == "D":
                    body["description"] = await t.ask("Description: ", 80)
                await s.post("/v1/admin/boards", body)
                t.line("Done.")
            elif ans in ("Q", ""):
                return
        except ApiError as exc:
            await _err(t, exc)


async def queues(t: Term, s: Session) -> None:
    try:
        q = await s.get("/v1/admin/queues")
    except ApiError as exc:
        return await _err(t, exc)
    t.title("Message queues")
    t.line(f"Live emergency bulletins: {q['live_emergency_bulletins']}")
    t.line("Forward queue: " + (", ".join(f"{c(k)}={v}" for k, v in q["forward_queue"].items()) or "empty"))
    for f in q["failed"]:
        t.line(f"  FAILED {c(f['mid'])[:30]} -> {c(f['peer_node'])} x{f['attempts']}: {c(f.get('last_error') or '')[:30]}")
    t.lines(wrap(q["note"]))
    await t.pause()


async def nodes(t: Term, s: Session) -> None:
    try:
        n = (await s.get("/v1/admin/nodes"))["nodes"]
    except ApiError as exc:
        return await _err(t, exc)
    t.title("Connected nodes")
    t.line(f"  {'NODE':<20} {'STATUS':<16} {'ADDRESS':<24} {'QUEUED':>6}  LAST SYNC")
    for x in n:
        if x["is_local"]:
            t.line(f"  {c(x['node_id']):<20} {'THIS NODE':<16} {'-':<24} {'-':>6}")
            continue
        status = c(x["status"]) if x["enabled"] else "DISABLED"
        last = c((x.get("last_sync_at") or "never").replace("T", " ").rstrip("Z"))
        t.line(f"  {c(x['node_id']):<20} {status:<16} {c(x.get('address') or '-')[:24]:<24} {x.get('waiting', 0):>6}  {last}")
        if x.get("last_error") and x["status"] != "CONNECTED":
            t.line(f"      last error: {c(x['last_error'])[:60]}")
    if len(n) == 1:
        t.line("No peers yet. On the Pi: sudo scripts/peer-card.sh to get this node's card,")
        t.line("and sudo scripts/add-peer.sh '<card>' to add a neighbour's (docs/mesh.md).")
    await t.pause()


async def health(t: Term, s: Session) -> None:
    full = await t.confirm("Run a full integrity check (slower)?")
    try:
        h = await s.get("/v1/admin/health", full=str(full).lower())
    except ApiError as exc:
        return await _err(t, exc)
    t.title("System health")
    cnt = h["counts"]
    t.line(f"Users: {cnt['active']} active, {cnt['pending']} pending, {cnt['suspended']} suspended")
    t.line(f"Bulletins: {cnt['bulletins']} visible, {cnt['hidden']} hidden.  Private messages: {cnt['private']}")
    t.line(f"Forward queue: {cnt['queue']} waiting, {cnt['failed']} failed")
    t.line("Sessions active (15 min): " + (", ".join(f"{c(k)}={v}" for k, v in h["sessions_active_15m"].items()) or "none"))
    d = h["database"]
    t.line(f"Database: {d['size_mb']} MB (+{d['wal_mb']} MB WAL), schema v{d['schema_version']}, "
           f"{'integrity' if full else 'quick'} check: {c(d.get('integrity_check') or d.get('quick_check'))}")
    for name, disk in h["disk"].items():
        if "free_mb" in disk:
            t.line(f"Disk ({name}): {disk['free_mb']} MB free of {disk['total_mb']} MB ({disk['used_pct']}% used)")
    m = h.get("memory") or {}
    if m:
        t.line(f"Memory: {m['available_mb']} MB available of {m['total_mb']} MB")
    up, hup = h["core_uptime_s"], h.get("host_uptime_s") or 0
    t.line(f"Uptime: core {up // 86400}d {up % 86400 // 3600}h, host {hup // 86400}d {hup % 86400 // 3600}h.  "
           f"Load: {' '.join(f'{x:.2f}' for x in h['load_avg'])}")
    sw = h.get("software") or {}
    line = f"Software: {c(sw.get('version', '?'))}"
    upd = sw.get("update") or {}
    if upd.get("state") == "available":
        line += f"  ** release {c(upd.get('latest'))} available: on the Pi run sudo scripts/update.sh **"
    elif upd.get("checked_at"):
        line += f"  (update check {c(upd.get('state'))}, {c(upd['checked_at'])[:10]})"
    t.line(line)
    await t.pause()


async def audit(t: Term, s: Session) -> None:
    before = None
    while True:
        try:
            data = await s.get("/v1/admin/audit", before=before, limit=15)
        except ApiError as exc:
            return await _err(t, exc)
        t.title("Audit log (newest first)")
        for e in data["entries"]:
            t.line(f"{e['id']:>6} {short_time(e['ts'])} {c(e.get('actor_name') or '-')[:10]:<10} "
                   f"{c(e['action'])[:22]:<22} {c(e['outcome']):<7} {c(e.get('target_type') or '')}:{c(e.get('target_id') or '')}")
        ans = (await t.readline(("N=older, " if data["more"] else "") + "ID=details, Q=back: ", 8)).upper()
        if ans == "N" and data["more"] and data["entries"]:
            before = data["entries"][-1]["id"]
        elif ans.isdigit():
            e = next((x for x in data["entries"] if x["id"] == int(ans)), None)
            if e:
                t.lines(wrap(json.dumps(e, indent=1)))
                await t.pause()
        elif ans in ("Q", ""):
            return


async def backups(t: Term, s: Session) -> None:
    try:
        b = await s.get("/v1/admin/backups")
    except ApiError as exc:
        return await _err(t, exc)
    t.title("Backup status")
    st = b.get("status") or {}
    if not st:
        t.line("No backup has run yet (scripts/backup.sh, daily timer scotland-bbs-backup.timer).")
    else:
        t.line(f"Last run:     {c(st.get('finished_at', '-'))}  result: {c(st.get('result', '-'))}")
        t.line(f"Last success: {c(st.get('last_success_at', '-'))}  file: {c(st.get('db_file', '-'))}")
        if st.get("error"):
            t.error(f"Last error: {st['error']}")
        if st.get("offsite"):
            t.line(f"Off-site copy: {c(st['offsite'])}")
    t.line("Recent database backups:")
    for f in b["recent_files"]:
        t.line(f"  {c(f['name'])}  {f['size_kb']} KB  {'checksum ok' if f['sha256_file'] else 'NO CHECKSUM'}")
    await t.pause()


async def config(t: Term, s: Session) -> None:
    try:
        cfg = await s.get("/v1/admin/config")
    except ApiError as exc:
        return await _err(t, exc)
    t.title("Configuration (read-only; edit config/*.yaml and restart bbs-core)")
    await t.page(wrap(json.dumps(cfg, indent=1)))


async def run(gw: Gateway, t: Term, s: Session, server: BBSServer) -> None:
    me = await s.get("/v1/sessions/me")
    t.line("")
    t.line(t.bold("BEACONNET - ADMINISTRATION"))
    t.line(f"Administrator: {c(me['username'])}   (every action here is recorded in the audit log)")
    menu = {
        "1": lambda: user_list(t, s, "PENDING", "Pending registrations"),
        "2": lambda: user_list(t, s, "ACTIVE", "Active users"),
        "3": lambda: user_list(t, s, "SUSPENDED", "Suspended users"),
        "4": lambda: moderation(t, s),
        "5": lambda: queues(t, s),
        "6": lambda: nodes(t, s),
        "7": lambda: health(t, s),
        "8": lambda: audit(t, s),
        "9": lambda: backups(t, s),
        "10": lambda: config(t, s),
    }
    while True:
        try:
            pending = (await s.get("/v1/admin/users", status="PENDING", limit=50))["users"]
        except ApiError as exc:
            if exc.status == 401:
                t.error("Session ended.")
                return
            pending = []
        t.line("")
        t.lines([f"1. Pending Registrations ({len(pending)}{'+' if len(pending) == 50 else ''})", "2. Active Users",
                 "3. Suspended Users", "4. Bulletin Moderation", "5. Message Queues", "6. Connected Nodes",
                 "7. System Health", "8. Audit Log", "9. Backup Status", "10. Configuration", "", "Q. Exit", ""])
        ans = (await t.readline("Selection: ", 5)).upper()
        if ans == "Q":
            t.line("Logged out.")
            return
        action = menu.get(ans)
        if action:
            try:
                await action()
            except ApiError as exc:
                if exc.status == 401:
                    t.error("Session ended.")
                    return
                await _err(t, exc)
