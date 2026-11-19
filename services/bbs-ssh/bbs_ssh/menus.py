"""Public BBS terminal: guest, pending and approved-user menus."""

from __future__ import annotations

from bbs_gateway.client import ApiError, Session
from bbs_gateway.sshserver import BBSServer, Gateway
from bbs_gateway.term import WIDTH, Disconnect, Term, box, c, short_time, wrap

PRIO_TAG = {"EMERGENCY": "[EMRG]", "PRIORITY": "[PRIO]", "ROUTINE": ""}
MAX_BODY = 8000


def _welcome(t: Term, info: dict, me: dict) -> None:
    t.line("")
    t.lines(box(["", "SCOTLAND EMERGENCY PACKET RADIO BBS", "", "Independent Communications", ""]))
    t.line("")
    t.line(f"Node: {c(info['node_id'])}")
    t.line(f"Status: {c(info['status'])}")
    t.line("Connection: SSH")
    if me.get("username"):
        call = f" ({c(me['callsign'])})" if me.get("callsign") else ""
        t.line(f"Logged in: {c(me['username'])}{call}  Account: {c(me['status'])}")
        if me.get("status") == "PENDING":
            t.line("Your registration is waiting for a sysop to approve it. You can read public")
            t.line("bulletins meanwhile; posting and mail open once you're approved.")
        elif me.get("status") == "SUSPENDED":
            t.line("This account is SUSPENDED: read-only access. Contact the sysop.")
        if me.get("unread_mail"):
            t.line(t.bold(f"You have {me['unread_mail']} new private message(s)."))


def _msg_line(m: dict, show_board: bool) -> str:
    mark = " " if m.get("read", True) else "*"
    who = c(m.get("callsign") or m.get("author") or "?")[:9]
    tag = PRIO_TAG.get(m.get("priority", "ROUTINE"), "")
    if m.get("expired"):
        tag = "[EXPD]"
    board = f"{c(m['board'])[:10]:<10} " if show_board else ""
    subj = c(m["subject"])
    head = f"{mark}{m['number']:>6} {short_time(m['created_at'])} {who:<9} {board}"
    room = WIDTH - 1 - len(head) - (len(tag) + 1 if tag else 0)
    return head + (tag + " " if tag else "") + subj[:max(10, room)]


async def _error(t: Term, exc: ApiError) -> None:
    t.error(exc.message)


# --- reading ---------------------------------------------------------------------------


async def read_message(t: Term, s: Session, number: int, me: dict) -> None:
    try:
        m = await s.get(f"/v1/messages/{number}")
    except ApiError as exc:
        return await _error(t, exc)
    lines = [
        "",
        t.bold(f"Msg #{m['number']}  Board: {c(m['board'])}"),
        f"From: {c(m['author'])}" + (f" ({c(m['callsign'])})" if m.get("callsign") else "")
        + ("" if "@" in m["author"] else f"  @ {c(m['origin_node'])}"),
        f"Date: {c(m['created_at'])}",
    ]
    if m.get("priority") and m["priority"] != "ROUTINE" or m.get("area"):
        lines.append(f"Priority: {c(m['priority'])}  Area: {c(m.get('area') or '-')}  Expires: {c(m.get('expires_at') or '-')}"
                     + ("  ** EXPIRED **" if m.get("expired") else ""))
    if m.get("parent_number"):
        lines.append(f"In reply to: #{m['parent_number']}")
    lines.append(f"Subj: {c(m['subject'])}")
    lines.append("-" * (WIDTH - 1))
    lines.extend(wrap(m["body"]))
    lines.append("-" * (WIDTH - 1))
    if m.get("disclaimer"):
        lines.extend(wrap(m["disclaimer"]))
    await t.page(lines)
    if me.get("can_post"):
        ans = (await t.readline("R=reply on board, M=reply by private mail, Enter=back: ", 5)).upper()
        if ans == "R":
            subj = m["subject"] if m["subject"].lower().startswith("re:") else f"Re: {m['subject']}"
            await post_flow(t, s, me, board=m["board"], reply_to=m["number"], subject=subj[:80])
        elif ans == "M":
            await compose_mail(t, s, to=m["author"], subject=f"Re: {m['subject']}"[:80])


async def list_messages(t: Term, s: Session, me: dict, board: str | None) -> None:
    before = None
    pages: list[int | None] = []
    while True:
        try:
            data = await s.get("/v1/messages", board=board, before=before, limit=15)
        except ApiError as exc:
            return await _error(t, exc)
        title = f"Board {board}" if board else "Recent bulletins"
        if data.get("board", {}).get("description"):
            title += f" - {data['board']['description']}"
        t.title(c(title))
        if data.get("disclaimer"):
            t.lines(wrap(data["disclaimer"]))
            t.line("")
        msgs = data["messages"]
        if not msgs:
            t.line("No messages here yet.")
        else:
            t.line(f" {'Msg#':>6} {'Date':<12} {'From':<9} " + ("Board      " if not board else "") + "Subject")
            for m in msgs:
                t.line(_msg_line(m, show_board=not board))
            if me.get("can_post"):
                t.line("(* = unread)")
        can_post = data.get("board", {}).get("can_post", False)
        opts = ["number=read"]
        if data.get("more"):
            opts.append("N=older")
        if pages:
            opts.append("B=newer")
        if can_post:
            opts.append("P=post")
        opts.append("Q=back")
        ans = (await t.readline(", ".join(opts) + ": ", 10)).upper()
        if ans.isdigit():
            await read_message(t, s, int(ans), me)
        elif ans == "N" and data.get("more") and msgs:
            pages.append(before)
            before = msgs[-1]["number"]
        elif ans == "B" and pages:
            before = pages.pop()
        elif ans == "P" and can_post and board:
            await post_flow(t, s, me, board=board)
        elif ans in ("Q", ""):
            return


async def browse_boards(t: Term, s: Session, me: dict) -> None:
    while True:
        try:
            boards = (await s.get("/v1/boards"))["boards"]
        except ApiError as exc:
            return await _error(t, exc)
        t.title("Bulletin boards")
        for i, b in enumerate(boards, 1):
            unread = f" ({b['unread']} new)" if b.get("unread") else ""
            flag = " [EMERGENCY]" if b["emergency"] else (" [read-only]" if not b["can_post"] and me.get("can_post") else "")
            t.line(f"{i:>3}. {c(b['name']):<14} {b['count']:>5} msgs{unread}{flag}")
            if b.get("description"):
                t.line(f"      {c(b['description'])[:70]}")
        ans = (await t.readline("Board number or name (Q=back): ", 24)).upper()
        if ans in ("Q", ""):
            return
        chosen = None
        if ans.isdigit() and 1 <= int(ans) <= len(boards):
            chosen = boards[int(ans) - 1]["name"]
        else:
            chosen = next((b["name"] for b in boards if b["name"].upper() == ans), None)
        if chosen:
            await list_messages(t, s, me, chosen)
        else:
            t.error("No such board.")


async def search_flow(t: Term, s: Session, me: dict) -> None:
    t.title("Search bulletins")
    t.line("Leave a field blank to skip it.")
    number = await t.ask("Message number: ", 10)
    board = (await t.ask("Board: ", 24)).upper()
    sender = await t.ask("Sender (username or callsign): ", 40)
    subject = await t.ask("Words in subject: ", 80)
    text = await t.ask("Words anywhere in the message: ", 80)
    if not any((number, board, sender, subject, text)):
        return
    if number and not number.isdigit():
        t.error("Message number must be a number.")
        return
    try:
        data = await s.get("/v1/messages/search", number=number or None, board=board or None, sender=sender or None,
                           subject=subject or None, text=text or None)
    except ApiError as exc:
        return await _error(t, exc)
    msgs = data["messages"]
    t.title(f"{len(msgs)} match(es)")
    for m in msgs:
        t.line(_msg_line(m, show_board=True))
    while msgs:
        ans = await t.readline("Number to read (Enter=back): ", 10)
        if not ans.isdigit():
            return
        await read_message(t, s, int(ans), me)


# --- posting ---------------------------------------------------------------------------


async def post_flow(t: Term, s: Session, me: dict, board: str | None = None, reply_to: int | None = None,
                    subject: str | None = None) -> None:
    if not me.get("can_post"):
        t.error("Posting needs an approved account.")
        return
    try:
        boards = {b["name"]: b for b in (await s.get("/v1/boards"))["boards"]}
    except ApiError as exc:
        return await _error(t, exc)
    if board is None:
        postable = [n for n, b in boards.items() if b["can_post"]]
        t.line("Boards you can post on: " + ", ".join(postable))
        board = (await t.ask("Board: ", 24)).upper()
    b = boards.get(board)
    if b is None or not b["can_post"]:
        t.error(f"You can't post on {board or 'that board'}.")
        return
    t.title(f"Post on {board}")
    extra: dict = {}
    if b["emergency"]:
        t.lines(wrap("EMERGENCY board: for real emergency and priority traffic only. This BBS is NOT an official "
                     "emergency service - in an emergency call 999 or 112. Traffic may later be relayed over amateur "
                     "radio, where it is public and must follow your licence conditions."))
        prio = (await t.ask("Priority - R=routine, P=priority, E=emergency [R]: ", 2)).upper()
        extra["priority"] = {"E": "EMERGENCY", "P": "PRIORITY"}.get(prio, "ROUTINE")
        extra["area"] = await t.ask("Geographic area (e.g. Highland, Glasgow, Skye): ", 40)
        hours = await t.ask("Expires after how many hours? [72]: ", 4)
        if hours:
            if not hours.isdigit():
                t.error("Hours must be a number.")
                return
            extra["expires_hours"] = int(hours)
    if subject:
        t.line(f"Subject: {c(subject)}")
        new = await t.ask("New subject (Enter keeps it): ", 80)
        subject = new or subject
    else:
        subject = await t.ask("Subject: ", 80)
    if not subject:
        t.error("Cancelled - no subject.")
        return
    body = await t.read_body(MAX_BODY)
    if not body:
        t.line("Cancelled.")
        return
    if not await t.confirm("Send this bulletin?", default=True):
        t.line("Not sent.")
        return
    try:
        r = await s.post("/v1/messages", {"board": board, "subject": subject, "body": body, "reply_to": reply_to, **extra})
    except ApiError as exc:
        return await _error(t, exc)
    t.line(f"Posted as message #{r['number']} on {c(r['board'])}.")


# --- private mail ----------------------------------------------------------------------


async def compose_mail(t: Term, s: Session, to: str | None = None, subject: str | None = None,
                       reply_to: int | None = None) -> None:
    t.title("New private message")
    t.lines(wrap("Only you and the recipient can read private messages in the BBS. Mail to another node "
                 "(user@NODE-ID) travels over encrypted node links, but each node it passes through stores it. "
                 "Nothing is encrypted end-to-end: don't send passwords or anything you wouldn't want a sysop to see."))
    to = to or await t.ask("To (username, callsign, or user@NODE-ID): ", 64)
    if not to:
        return
    t.line(f"To: {c(to)}")
    subject = subject or await t.ask("Subject: ", 80)
    if not subject:
        return
    t.line(f"Subject: {c(subject)}")
    body = await t.read_body(MAX_BODY)
    if not body:
        t.line("Cancelled.")
        return
    try:
        r = await s.post("/v1/mail", {"to": to, "subject": subject, "body": body, "reply_to": reply_to})
    except ApiError as exc:
        return await _error(t, exc)
    t.line(f"Sent to {c(r['to'])} - {c(r['delivery_state'])}.")


async def read_mail(t: Term, s: Session, pm_id: int) -> None:
    try:
        m = await s.get(f"/v1/mail/{pm_id}")
    except ApiError as exc:
        return await _error(t, exc)
    lines = ["", t.bold(f"Private message {m['id']}"), f"From: {c(m['from'])}", f"To:   {c(m['to'])}",
             f"Date: {c(m['created_at'])}  State: {c(m['delivery_state'])}", f"Subj: {c(m['subject'])}",
             "-" * (WIDTH - 1), *wrap(m["body"]), "-" * (WIDTH - 1)]
    await t.page(lines)
    ans = (await t.readline("R=reply, A=archive, D=delete, Enter=back: ", 5)).upper()
    try:
        if ans == "R":
            other = m["from"] if m["folder"] == "inbox" else m["to"]
            subj = m["subject"] if m["subject"].lower().startswith("re:") else f"Re: {m['subject']}"
            await compose_mail(t, s, to=other, subject=subj[:80], reply_to=m["id"])
        elif ans == "A":
            await s.post(f"/v1/mail/{pm_id}/state", {"state": "ARCHIVED"})
            t.line("Archived.")
        elif ans == "D" and await t.confirm("Delete this message?"):
            await s.post(f"/v1/mail/{pm_id}/state", {"state": "DELETED"})
            t.line("Deleted.")
    except ApiError as exc:
        await _error(t, exc)


async def mail_menu(t: Term, s: Session, me: dict) -> None:
    folder = "inbox"
    while True:
        try:
            data = await s.get("/v1/mail", folder=folder, limit=15)
        except ApiError as exc:
            return await _error(t, exc)
        t.title(f"Private mail - {folder} ({data['unread']} unread in inbox)")
        if not data["messages"]:
            t.line("Nothing here.")
        for m in data["messages"]:
            mark = "*" if folder == "inbox" and not m["read"] else " "
            other = m["from"] if m["folder"] == "inbox" else f"to {m['to']}"
            t.line(f"{mark}{m['id']:>6} {short_time(m['created_at'])} {c(other)[:14]:<14} {c(m['subject'])[:44]}")
        ans = (await t.readline("number=read, I=inbox, S=sent, A=archive, C=compose, Q=back: ", 10)).upper()
        if ans.isdigit():
            await read_mail(t, s, int(ans))
        elif ans == "I":
            folder = "inbox"
        elif ans == "S":
            folder = "sent"
        elif ans == "A":
            folder = "archive"
        elif ans == "C":
            await compose_mail(t, s)
        elif ans in ("Q", ""):
            return


# --- account ---------------------------------------------------------------------------


async def account_menu(t: Term, s: Session) -> None:
    while True:
        try:
            me = await s.get("/v1/sessions/me")
            keys = (await s.get("/v1/account/keys"))["keys"]
        except ApiError as exc:
            return await _error(t, exc)
        t.title("My account")
        t.line(f"Username:     {c(me['username'])}")
        call = c(me.get("callsign") or "-")
        if me.get("callsign"):
            call += " (verified)" if me.get("callsign_verified") else " (not yet verified by a sysop)"
        t.line(f"Callsign:     {call}")
        t.line(f"Display name: {c(me.get('display_name') or '-')}")
        t.line(f"Status:       {c(me['status'])}" + (f" - {c(me['status_reason'])}" if me.get("status_reason") else ""))
        t.line(f"Member since: {c(me.get('created_at'))}")
        t.line("")
        t.line("SSH keys:")
        for k in keys:
            state = f"revoked {short_time(k['revoked_at'])}" if k["revoked_at"] else f"last used {short_time(k['last_used_at'])}"
            t.line(f"  {k['id']:>4}. {c(k['type'])[:11]:<11} {c(k['fingerprint'])[:50]}")
            t.line(f"        {c(k.get('comment') or '')[:30]:<30} {state}")
        if me["status"] != "ACTIVE":
            await t.pause()
            return
        ans = (await t.readline("A=add a key, R=revoke a key, Q=back: ", 5)).upper()
        try:
            if ans == "A":
                t.line("Paste the new public key (one line, e.g. contents of ~/.ssh/id_ed25519.pub):")
                key = await t.readline("> ", 8000)
                if key:
                    r = await s.post("/v1/account/keys", {"public_key": key})
                    t.line(f"Added {c(r['fingerprint'])}.")
            elif ans == "R":
                kid = await t.ask("Key number to revoke: ", 6)
                if kid.isdigit() and await t.confirm(f"Revoke key {kid}? Sessions using it end immediately."):
                    await s.delete(f"/v1/account/keys/{kid}")
                    t.line("Revoked.")
            else:
                return
        except ApiError as exc:
            await _error(t, exc)


# --- registration ----------------------------------------------------------------------


async def register_flow(gw: Gateway, t: Term, s: Session) -> None:
    t.title("Register a new account")
    t.lines(wrap(
        "Every account is approved by a sysop before it can post or send mail. You log in with an SSH key, "
        "not a password: paste your PUBLIC key (the .pub file, e.g. ~/.ssh/id_ed25519.pub). "
        "No key yet? On your computer run:  ssh-keygen -t ed25519"))
    t.lines(wrap("Callsigns are checked by the sysop before they are shown as verified. "
                 "Contact details are optional and only visible to sysops."))
    t.line("")
    vals = {"username": "", "callsign": "", "display_name": "", "contact": "", "public_key": ""}
    for attempt in range(3):
        def dflt(k: str) -> str:
            return f" [{vals[k][:20]}]" if vals[k] else ""
        vals["username"] = (await t.ask(f"Username (3-20: a-z 0-9 _ -){dflt('username')}: ", 20, vals["username"])).lower()
        vals["callsign"] = (await t.ask(f"Amateur callsign (blank if none){dflt('callsign')}: ", 10, vals["callsign"])).upper()
        vals["display_name"] = await t.ask(f"Display name (optional){dflt('display_name')}: ", 40, vals["display_name"])
        vals["contact"] = await t.ask(f"Contact details, private (optional){dflt('contact')}: ", 120, vals["contact"])
        t.line("Paste your SSH public key on one line" + (" (Enter keeps the one you gave)" if vals["public_key"] else "") + ":")
        vals["public_key"] = await t.ask("> ", 8000, vals["public_key"])
        if not vals["username"] or not vals["public_key"]:
            t.error("Username and public key are required.")
            if not await t.confirm("Try again?", default=True):
                return
            continue
        t.line("")
        t.line(f"Username: {c(vals['username'])}   Callsign: {c(vals['callsign'] or '-')}")
        t.line(f"Display:  {c(vals['display_name'] or '-')}   Contact: {c(vals['contact'] or '-')}")
        if not await t.confirm("Submit this registration?", default=True):
            return
        try:
            r = await s.post("/v1/registrations", {k: (v or None) for k, v in vals.items()})
        except ApiError as exc:
            t.error(exc.message)
            if exc.code == "rate_limited" or attempt == 2 or not await t.confirm("Correct and try again?", default=True):
                return
            continue
        t.line("")
        t.line(t.bold("Registration received - status PENDING."))
        t.line(f"Key fingerprint: {c(r['fingerprint'])}")
        t.lines(wrap(f"A sysop will review it. Log in with your key at any time to see your status "
                     f"(read-only until approved):   ssh -p {gw.cfg.public_port} {c(r['username'])}@{c(gw.cfg.public_host)}"))
        await t.pause()
        return


# --- information -----------------------------------------------------------------------


async def system_info(t: Term, s: Session) -> None:
    try:
        i = await s.get("/v1/system/info")
    except ApiError as exc:
        return await _error(t, exc)
    t.title("System information")
    for label, key in (("Node", "node_id"), ("Name", "node_name"), ("Location", "location"), ("Sysop", "sysop"),
                       ("Status", "status"), ("Software", "version"), ("Time (UTC)", "time_utc")):
        if i.get(key):
            t.line(f"{label + ':':<14}{c(i[key])}")
    up = int(i.get("uptime_s", 0))
    t.line(f"{'Uptime:':<14}{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m")
    t.line(f"{'Users:':<14}{i['active_users']} approved   Bulletins: {i['bulletins']}   Boards: {i['boards']}")
    t.line(f"{'Registration:':<14}{'open (sysop approval required)' if i['registration_open'] else 'closed'}")
    t.line("")
    t.lines(wrap(i["disclaimer"]))
    await t.pause()


async def help_screen(t: Term, gw: Gateway) -> None:
    h, p = c(gw.cfg.public_host), gw.cfg.public_port
    text = f"""HELP

Connecting
  Visitors:        ssh -p {p} guest@{h}
  Register:        ssh -p {p} new@{h}     (or option 3 from the guest menu)
  Approved users:  ssh -p {p} <username>@{h}   with your registered SSH key

Menus
  Type the number or letter shown and press Enter. Q goes back or disconnects.
  At the menu prompt you can also type:  R <number>  read a message,
  L  list recent bulletins,  A  turn ANSI bold on/off.

Writing messages
  Type your text line by line. End with /EX on a line by itself (traditional
  packet-BBS style; '.' also works). /ABORT cancels.

Rules
  Guests and pending accounts can read public boards. Posting and private mail
  need an account approved by a sysop. Be courteous: this is a shared, low-
  bandwidth system. Messages may later be relayed over amateur radio, where
  they are public and subject to licence conditions - no business traffic,
  no encryption, nothing confidential.

Emergencies
  This BBS is not an official emergency service and delivery is not
  guaranteed. In an emergency call 999 or 112.
"""
    await t.page(text.splitlines())


# --- main menu -------------------------------------------------------------------------


async def run(gw: Gateway, t: Term, s: Session, server: BBSServer) -> None:
    info = await s.get("/v1/system/info")
    me = await s.get("/v1/sessions/me")
    _welcome(t, info, me)
    while True:
        me = await s.get("/v1/sessions/me")
        t.line("")
        if me.get("can_post"):
            mail = f" ({me['unread_mail']} new)" if me.get("unread_mail") else ""
            items = ["1. Read Recent Bulletins", "2. Browse Bulletin Boards", "3. Post a Bulletin",
                     "4. Search Bulletins", f"5. Private Mail{mail}", "6. My Account & Keys",
                     "7. System Information", "8. Help"]
        else:
            third = "3. Register New Account" if me["kind"] == "guest" else "3. My Account Status"
            items = ["1. Read Public Bulletins", "2. Browse Bulletin Boards", third, "4. User Login",
                     "5. System Information", "6. Help"]
        t.lines(items)
        t.line("")
        t.line("Q. Disconnect")
        t.line("")
        raw = await t.readline("Selection: ", 20)
        ans = raw.upper()
        words = ans.split()
        if ans in ("Q", "B", "BYE"):
            t.line("73 - goodbye.")
            return
        if words and words[0] == "R" and len(words) == 2 and words[1].isdigit():
            await read_message(t, s, int(words[1]), me)
            continue
        if ans == "L":
            await list_messages(t, s, me, None)
            continue
        if ans == "A":
            if t.has_pty:
                t.ansi = not t.ansi
                t.line(f"ANSI {'on' if t.ansi else 'off'}.")
            continue
        try:
            if me.get("can_post"):
                actions = {
                    "1": lambda: list_messages(t, s, me, None), "2": lambda: browse_boards(t, s, me),
                    "3": lambda: post_flow(t, s, me), "4": lambda: search_flow(t, s, me),
                    "5": lambda: mail_menu(t, s, me), "6": lambda: account_menu(t, s),
                    "7": lambda: system_info(t, s), "8": lambda: help_screen(t, gw),
                }
            else:
                actions = {
                    "1": lambda: list_messages(t, s, me, None), "2": lambda: browse_boards(t, s, me),
                    "3": (lambda: register_flow(gw, t, s)) if me["kind"] == "guest" else (lambda: account_menu(t, s)),
                    "4": lambda: login_help(t, gw, me), "5": lambda: system_info(t, s), "6": lambda: help_screen(t, gw),
                }
            action = actions.get(ans)
            if action is None:
                if ans:
                    t.error("Choose one of the options shown.")
                continue
            await action()
        except ApiError as exc:
            if exc.status == 401:
                t.error("Your session has ended. Please reconnect.")
                return
            await _error(t, exc)
        except Disconnect:
            raise


async def login_help(t: Term, gw: Gateway, me: dict) -> None:
    t.title("User login")
    if me.get("username"):
        t.line(f"You are logged in as {c(me['username'])}; account status {c(me['status'])}.")
    else:
        t.lines(wrap("Logging in uses your SSH key, so it happens when you connect rather than from this menu. "
                     "Disconnect, then connect with your username:"))
        t.line("")
        t.line(f"    ssh -p {gw.cfg.public_port} <username>@{c(gw.cfg.public_host)}")
        t.line("")
        t.lines(wrap("Your SSH client offers your key automatically. If you see 'Permission denied (publickey)', "
                     "the key isn't the one you registered - use  ssh -i <keyfile> ..."))
    await t.pause()


async def register_only(gw: Gateway, t: Term, s: Session) -> None:
    """Session for 'ssh new@host': straight into registration."""
    info = await s.get("/v1/system/info")
    me = await s.get("/v1/sessions/me")
    _welcome(t, info, me)
    if not info.get("registration_open"):
        t.error("Registration is closed on this node.")
        return
    await register_flow(gw, t, s)
    t.line("73 - goodbye.")
