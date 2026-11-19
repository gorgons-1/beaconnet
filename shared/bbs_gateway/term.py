"""Line-based terminal I/O for 80-column, low-bandwidth sessions.

Output is plain text with optional, minimal ANSI (bold/reverse only). Nothing typed by
a user is ever written out unfiltered: c() removes every control character, so posted
text can't carry terminal escape sequences to other users.
"""

from __future__ import annotations

import asyncio
import re
import textwrap
import unicodedata

import asyncssh

WIDTH = 80
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class Disconnect(Exception):
    """The user left, timed out or the connection dropped."""


def c(text: object) -> str:
    """Make any value safe to print: no control characters, no invisible format chars."""
    s = "" if text is None else str(text)
    s = _CONTROL.sub("", s.replace("\r", ""))
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")


def wrap(text: str, width: int = WIDTH - 1) -> list[str]:
    out: list[str] = []
    for line in c(text).split("\n"):
        if not line.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(line, width=width, replace_whitespace=False, drop_whitespace=True,
                                 break_long_words=True, break_on_hyphens=False) or [""])
    return out


def box(lines: list[str], width: int = 62) -> list[str]:
    inner = width - 2
    out = ["+" + "-" * inner + "+"]
    for ln in lines:
        out.append("|" + c(ln).center(inner)[:inner] + "|")
    out.append("+" + "-" * inner + "+")
    return out


def short_time(iso: str | None) -> str:
    """'2026-11-18T14:02:00Z' -> '18-Nov 14:02'."""
    if not iso or len(iso) < 16:
        return "-"
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    try:
        return f"{iso[8:10]}-{months[int(iso[5:7]) - 1]} {iso[11:16]}"
    except (ValueError, IndexError):
        return iso[:16]


class Term:
    def __init__(self, process: asyncssh.SSHServerProcess, idle_timeout: int = 600) -> None:
        self.p = process
        term_type = process.get_terminal_type()
        self.has_pty = term_type is not None
        self.ansi = self.has_pty and term_type not in ("dumb", "unknown")
        _, rows, _, _ = process.get_terminal_size()
        self.rows = rows if rows and rows >= 10 else 24
        self.idle_timeout = idle_timeout

    # --- output ------------------------------------------------------------------

    def write(self, text: str = "") -> None:
        """Write our own text (already safe). On a pty, asyncssh's line editor turns
        newlines into CRLF itself."""
        try:
            self.p.stdout.write(text)
        except (asyncssh.Error, OSError, BrokenPipeError) as exc:
            raise Disconnect() from exc

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def lines(self, lines: list[str]) -> None:
        self.write("\n".join(lines) + "\n")

    def bold(self, s: str) -> str:
        return f"\x1b[1m{s}\x1b[0m" if self.ansi else s

    def rev(self, s: str) -> str:
        return f"\x1b[7m{s}\x1b[0m" if self.ansi else s

    def title(self, s: str) -> None:
        self.line("")
        self.line(self.bold(c(s)))
        self.line("-" * min(len(s), WIDTH - 1))

    def error(self, msg: str) -> None:
        self.line(("*** " if not self.ansi else "\x1b[1m*** ") + c(msg) + ("\x1b[0m" if self.ansi else ""))

    # --- input -------------------------------------------------------------------

    async def readline(self, prompt: str = "", max_len: int = 200) -> str:
        if prompt:
            self.write(prompt)
        while True:
            try:
                data = await asyncio.wait_for(self.p.stdin.readline(), timeout=self.idle_timeout)
            except asyncio.TimeoutError as exc:
                self.line("\n\nIdle timeout - disconnecting. 73!")
                raise Disconnect() from exc
            except asyncssh.TerminalSizeChanged as exc:
                if exc.height and exc.height >= 10:
                    self.rows = exc.height
                continue
            except (asyncssh.BreakReceived, asyncssh.SignalReceived) as exc:
                raise Disconnect() from exc
            except (asyncssh.Error, OSError, ConnectionError) as exc:
                raise Disconnect() from exc
            break
        if data == "":
            raise Disconnect()
        text = c(data.rstrip("\r\n"))
        if "\x04" in data:  # Ctrl-D
            raise Disconnect()
        return text.strip()[:max_len]

    async def ask(self, prompt: str, max_len: int = 200, default: str | None = None) -> str:
        answer = await self.readline(prompt, max_len)
        return answer if answer else (default or "")

    async def confirm(self, prompt: str, default: bool = False) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        ans = (await self.readline(f"{prompt} {hint}: ", 5)).lower()
        if not ans:
            return default
        return ans.startswith("y")

    async def pause(self) -> None:
        await self.readline("-- press Enter --", 5)

    async def page(self, lines: list[str]) -> bool:
        """Show lines a screen at a time. Returns False if the user quit early."""
        per = max(8, self.rows - 2)
        for i in range(0, len(lines), per):
            self.lines(lines[i:i + per])
            if i + per < len(lines):
                ans = (await self.readline(self.rev("-- More: Enter=next, Q=stop --") + " ", 5)).lower()
                if ans.startswith("q"):
                    return False
        return True

    async def read_body(self, max_bytes: int) -> str | None:
        """Multi-line entry, packet-BBS style: end with /EX (or '.') on its own line,
        /ABORT to cancel. Returns None if cancelled."""
        self.line("Enter your message. End with /EX on a line by itself (or /ABORT to cancel).")
        lines: list[str] = []
        size = 0
        while True:
            ln = await self.readline("", 400)
            up = ln.strip().upper()
            if up in ("/EX", ".", "***END"):
                break
            if up == "/ABORT":
                return None
            size += len(ln.encode()) + 1
            if size > max_bytes:
                self.error(f"Message limit is {max_bytes} bytes - stopped here. /EX to send, /ABORT to cancel.")
                continue
            lines.append(ln)
        return "\n".join(lines).strip("\n")
