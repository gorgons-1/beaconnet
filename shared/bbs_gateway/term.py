"""Line-based terminal I/O for 80- or 40-column (C64-style), low-bandwidth sessions.

Output is plain text with optional, minimal ANSI (bold/reverse only). Nothing typed by
a user is ever written out unfiltered: c() removes every control character, so posted
text can't carry terminal escape sequences to other users.
"""

from __future__ import annotations

import asyncio
import re
import textwrap
import unicodedata
from contextvars import ContextVar

import asyncssh

WIDTH = 80
NARROW = 40                 # Commodore 64 / small screens
MIN_WIDTH, MAX_WIDTH = 32, 80
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Terminal types that mean "8-bit micro": plain ASCII, no ANSI, 40 columns unless told otherwise.
RETRO_TERMS = ("c64", "c128", "commodore", "petscii", "vic20", "plus4")
_ASCII_MAP = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
                            "\u2014": "-", "\u2026": "...", "\u00a0": " ", "\u00a3": "L", "\u20ac": "EUR",
                            "\u00df": "ss", "\u00e6": "ae", "\u00c6": "AE", "\u00f8": "o", "\u00d8": "O"})
_cols: ContextVar[int] = ContextVar("bbs_cols", default=WIDTH)


def cols() -> int:
    """Screen width of the current session (80, or 40 on a C64)."""
    return _cols.get()


def rule() -> str:
    return "-" * (cols() - 1)


def to_ascii(text: str) -> str:
    """Fold to 7-bit ASCII for 8-bit terminals: accents dropped, smart quotes straightened."""
    s = unicodedata.normalize("NFKD", text.translate(_ASCII_MAP))
    return "".join(ch if ord(ch) < 128 else ("" if unicodedata.combining(ch) else "?") for ch in s)


def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


class Disconnect(Exception):
    """The user left, timed out or the connection dropped."""


def c(text: object) -> str:
    """Make any value safe to print: no control characters, no invisible format chars."""
    s = "" if text is None else str(text)
    s = _CONTROL.sub("", s.replace("\r", ""))
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")


def wrap(text: str, width: int | None = None) -> list[str]:
    width = width or cols() - 1
    out: list[str] = []
    for line in c(text).split("\n"):
        if not line.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(line, width=width, replace_whitespace=False, drop_whitespace=True,
                                 break_long_words=True, break_on_hyphens=False) or [""])
    return out


def box(lines: list[str], width: int | None = None) -> list[str]:
    width = width or min(62, cols() - 1)
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
        term_type = (process.get_terminal_type() or "").lower() or None
        self.has_pty = term_type is not None
        retro = bool(term_type) and any(r in term_type for r in RETRO_TERMS)
        self.ansi = self.has_pty and term_type not in ("dumb", "unknown") and not retro
        width, rows, _, _ = process.get_terminal_size()
        self.rows = rows if rows and rows >= 10 else 24
        self.idle_timeout = idle_timeout
        self.width_fixed = False
        self.ascii = retro
        self.set_width(NARROW if retro and not (width and width < WIDTH) else self._fit_cols(width))

    @staticmethod
    def _fit_cols(width: int | None) -> int:
        if not width or width >= WIDTH:
            return WIDTH
        return max(MIN_WIDTH, width)

    def set_width(self, width: int, fixed: bool = False) -> None:
        """40-ish columns also means plain ASCII: 8-bit terminals have no Unicode."""
        self.width = width
        self.width_fixed = self.width_fixed or fixed
        if width < WIDTH:
            self.ascii = True
        _cols.set(width)

    @property
    def narrow(self) -> bool:
        return self.width < WIDTH

    def fit(self, text: str) -> str:
        """Word-wrap every line to the screen, leaving the last column free (a C64 moves to
        the next row by itself after column 40, which would double-space the output)."""
        if self.ascii:
            text = to_ascii(text)
        if not self.narrow:
            return text
        return "\n".join(self._fold(ln) for ln in text.split("\n"))

    def _fold(self, line: str) -> str:
        room = self.width - 1
        if vlen(line) <= room:
            return line
        lead = len(line) - len(line.lstrip(" "))
        cont = " " * min(lead + 2, 8)
        out: list[str] = []
        cur = line[:lead]
        for word in line[lead:].split(" "):
            while vlen(word) > room - len(cont):          # a word too long for any row: hard-break it
                take = room - vlen(cur) - (1 if vlen(cur.strip()) else 0)
                if take < 4:
                    out.append(cur.rstrip())
                    cur, take = cont, room - len(cont)
                sep = " " if cur.strip() else ""
                out.append(cur + sep + word[:take])
                cur, word = cont, word[take:]
            sep = " " if cur.strip() else ""
            if vlen(cur) + len(sep) + vlen(word) > room:
                out.append(cur.rstrip())
                cur = cont + word
            else:
                cur = cur + sep + word
        out.append(cur)
        return "\n".join(out)

    # --- output ------------------------------------------------------------------

    def write(self, text: str = "") -> None:
        """Write our own text (already safe). On a pty, asyncssh's line editor turns
        newlines into CRLF itself."""
        try:
            self.p.stdout.write(self.fit(text))
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
        self.line("-" * min(len(s), self.width - 1))

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
                if exc.width and not self.width_fixed:
                    self.set_width(self._fit_cols(exc.width))
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

    async def secret(self, prompt: str, max_len: int = 256) -> str:
        """Read a line without echoing it (passwords). Unlike readline, keeps spaces and
        doesn't sanitise: the text goes straight to bbs-core, never to the screen."""
        echo_off = False
        if self.has_pty:
            try:
                self.p.channel.set_echo(False)
                echo_off = True
            except Exception:
                pass
        try:
            self.write(prompt)
            while True:
                try:
                    data = await asyncio.wait_for(self.p.stdin.readline(), timeout=self.idle_timeout)
                except asyncio.TimeoutError as exc:
                    self.line("\n\nIdle timeout - disconnecting. 73!")
                    raise Disconnect() from exc
                except asyncssh.TerminalSizeChanged:
                    continue
                except (asyncssh.BreakReceived, asyncssh.SignalReceived, asyncssh.Error, OSError, ConnectionError) as exc:
                    raise Disconnect() from exc
                break
        finally:
            if echo_off:
                try:
                    self.p.channel.set_echo(True)
                except Exception:
                    pass
        if echo_off:
            self.line("")
        if data == "" or "\x04" in data:
            raise Disconnect()
        return data.rstrip("\r\n")[:max_len]

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
        if self.narrow:                     # count the rows the wrapped lines really take
            lines = [r for ln in lines for r in self.fit(ln).split("\n")]
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
