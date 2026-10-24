"""Ausführung einzelner Schritte, unabhängig von der Oberfläche."""

from __future__ import annotations

import asyncio
import os
import re

from rich.markup import escape

from .jobs import CommandStep, PythonStep, StepContext

# badblocks überschreibt Zeilen mit Backspaces, ddrescue mit \r und ANSI-Sequenzen.
LINE_SPLIT = re.compile(r"[\r\n\b]+")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Englische Ausgaben, damit die Regexe in jobs.py greifen.
COMMAND_ENV = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}


def handle_line(step: CommandStep, ctx: StepContext, line: str, status) -> str | None:
    """Verteilt eine Ausgabezeile auf Fortschritt, Status oder Log.

    Gibt eine Fehlermeldung zurück, wenn die Zeile auf `fail_re` passt.
    """
    line = ANSI.sub("", line).rstrip()
    if not line.strip():
        return None
    if step.progress_re and (match := step.progress_re.search(line)):
        ctx.progress(float(match.group(1)))
    if step.status_re and step.status_re.search(line):
        status(line.strip())
    else:
        ctx.log(escape(line))
    if step.fail_re and step.fail_re.search(line):
        return line.strip()
    return None


async def run_command(step: CommandStep, ctx: StepContext, status) -> str | None:
    ctx.log(f"[dim]$ {escape(' '.join(step.argv))}[/]")
    try:
        process = await asyncio.create_subprocess_exec(
            *step.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=COMMAND_ENV,
            cwd=step.cwd,
        )
    except FileNotFoundError:
        return f"Programm nicht gefunden: {step.argv[0]}"
    ctx.process = process
    failure = None
    buffer = ""
    assert process.stdout is not None
    while chunk := await process.stdout.read(4096):
        buffer += chunk.decode(errors="replace")
        *lines, buffer = LINE_SPLIT.split(buffer)
        for line in lines:
            failure = handle_line(step, ctx, line, status) or failure
    failure = handle_line(step, ctx, buffer, status) or failure
    code = await process.wait()
    ctx.process = None
    if ctx.cancel.is_set():
        return "abgebrochen"
    if code not in step.ok_codes:
        return failure or f"Exit-Code {code}"
    return failure


async def run_python(step: PythonStep, ctx: StepContext) -> str | None:
    try:
        return await asyncio.to_thread(step.func, ctx)
    except OSError as exc:
        return f"{exc.strerror or exc} ({exc.filename or ''})".strip()
