#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""rt attach in a real terminal, through the running server, to real Zellij on an enrolled machine.

Drives `docker exec -it CLIENT rt attach ...` inside a pseudo-terminal, typing into it as a person
would. CLIENT is run.sh's client container (rt signed in, Zellij 0.45.1); BROKER is the stack's
broker container, restarted once to drop the connection.

  native_attach.py CLIENT BROKER MACHINE OTHER_MACHINE

Proves that an unknown session name is refused with the existing sessions listed and nothing created;
that rt attach reaches the shell of the named machine; that a dropped connection reconnects by
itself to the same shell; that detaching (Ctrl+Alt+g, then d) ends rt attach without reconnecting;
and that --create starts a new session on the other machine.
"""
import fcntl
import os
import pty
import re
import secrets
import select
import struct
import subprocess
import sys
import termios
import threading
import time

ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
DETACH = b"\x1b\x07"  # Ctrl+Alt+g


def check(condition, message):
    if not condition:
        print("FAIL " + message, file=sys.stderr, flush=True)
        sys.exit(1)
    print("PASS " + message, flush=True)


class Terminal:
    """One command in a real pseudo-terminal, read and typed into as a person would."""

    def __init__(self, *argv):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
            os.execvp(argv[0], argv)
        self.output, self.status = bytearray(), None
        self.lock = threading.Lock()
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 1)
                chunk = os.read(self.fd, 65536) if ready else None
            except OSError:
                chunk = b""
            if chunk == b"":
                return
            if chunk:
                with self.lock:
                    self.output += chunk

    def mark(self):
        with self.lock:
            return len(self.output)

    def since(self, mark=0):
        with self.lock:
            return ANSI.sub(b"", bytes(self.output[mark:])).decode(errors="replace")

    def type(self, text):
        for char in text:
            os.write(self.fd, char.encode())
            time.sleep(0.03)

    def wait(self, text, timeout=60, since=0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.since(since):
                return True
            if self.exited():
                break
            time.sleep(0.2)
        print("Terminal never showed %r; it shows:\n%s" % (text, self.since(since)[-2000:]), file=sys.stderr)
        return False

    def exited(self, timeout=0):
        deadline = time.monotonic() + timeout
        while self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
            elif time.monotonic() >= deadline:
                return False
            else:
                time.sleep(0.2)
        return True

    def detach(self):
        os.write(self.fd, DETACH)
        time.sleep(0.5)
        os.write(self.fd, b"d")

    def close(self):
        if not self.exited():
            os.kill(self.pid, 9)
            self.exited(5)
        os.close(self.fd)


def main(client, broker, machine, other):
    terminals = []

    def attach(*argv):
        terminal = Terminal("docker", "exec", "-it", "-e", "TERM=xterm-256color", client,
                            "python3", "/src/rt", "attach", *argv)
        terminals.append(terminal)
        return terminal

    def shell_on(terminal, name, mark=0):
        """The shell answers, and it runs on the named machine."""
        word = "rk-" + secrets.token_hex(3)
        if not terminal.wait("$ ", since=mark):
            return False
        mark = terminal.mark()
        terminal.type('echo "%s-$E2E_MACHINE-$((6*7))"\r' % word)
        return terminal.wait("%s-%s-42" % (word, name), since=mark)

    try:
        terminal = attach("nosuch", "--machine", machine)
        check(terminal.exited(30) and terminal.status != 0, "rt attach to an unknown session name exits with an error")
        refusal = terminal.since()
        check("No session nosuch" in refusal and "main" in refusal and "--create" in refusal,
              "the refusal lists the existing session main and names --create")

        terminal = attach("main", "--machine", machine)
        check(shell_on(terminal, machine), "rt attach main --machine %s reaches that machine's shell" % machine)
        kept = "kept-" + secrets.token_hex(3)
        mark = terminal.mark()
        terminal.type("kept=%s\r" % kept)
        time.sleep(1)

        subprocess.run(["docker", "restart", broker], check=True, capture_output=True, timeout=120)
        check(terminal.wait("retrying", since=mark, timeout=60), "a dropped connection makes rt attach retry")
        mark = terminal.mark()
        check(shell_on(terminal, machine, mark), "rt attach reconnected by itself")
        mark = terminal.mark()
        terminal.type('echo "$kept-$((6*7))"\r')
        check(terminal.wait(kept + "-42", since=mark), "the reconnected terminal is the same shell, its state intact")

        mark = terminal.mark()
        terminal.detach()
        check(terminal.exited(30) and terminal.status == 0, "detaching (Ctrl+Alt+g, then d) ends rt attach")
        after = terminal.since(mark)
        check("Detached" in after and "retrying" not in after, "rt attach says it detached and does not reconnect")

        terminal = attach("build", "--machine", other, "--create")
        check(shell_on(terminal, other), "rt attach build --create starts a new session on " + other)
        terminal.detach()
        check(terminal.exited(30) and terminal.status == 0, "detaching from the new session ends rt attach")
    finally:
        for terminal in terminals:
            terminal.close()


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
