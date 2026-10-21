"""Save progress reporter — edits the existing status message in place.

Reuses the EXACT five-segment bar renderer (▰/▱) from the model tester
(``ai_test_progress.render_progress_bar``). The reporter is a sync callable
factory: Telethon's ``download_media`` / ``send_file`` call
``progress_callback(received, total)`` synchronously, so the reporter builds a
closure that deduplicates by percentage, maps to the required milestones, and
edits the status message through an async-safe edit function.

The edit callable is ``async (text) -> bool``; the reporter schedules the edit
on the event loop (``asyncio.get_event_loop().create_task``) so the synchronous
Telethon callback never blocks the transfer. Edit failures are logged and never
propagate — a broken status edit must not abort a valid transfer.

Milestone policy (per the owner's required behavior):
  - files <= 10 MiB: report 50% and 100% during EACH download and EACH upload
  - files > 10 MiB: report 20%, 40%, 60%, 80%, 100% during EACH phase
  - unknown total: honest indeterminate state until the phase boundary
  - album members: each member reports its own download/upload progress,
    prefixed ``File N/total``
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from backend.bot.handlers.ai_test_progress import render_progress_bar

logger = logging.getLogger(__name__)

_SMALL_FILE_THRESHOLD = 10 * 1024 * 1024
_SMALL_MILESTONES = (0.5, 1.0)
_LARGE_MILESTONES = (0.2, 0.4, 0.6, 0.8, 1.0)
_EDIT_COALESCE_THRESHOLD = 0.0


def _milestones(total: int) -> tuple[float, ...]:
    if total <= 0:
        return ()
    if total <= _SMALL_FILE_THRESHOLD:
        return _SMALL_MILESTONES
    return _LARGE_MILESTONES


def _format_phase(phase: str) -> str:
    return "Downloading" if phase == "download" else "Uploading"


def _render_status(
    phase: str,
    fraction: float,
    member_index: int | None,
    member_total: int | None,
) -> str:
    bar = render_progress_bar(fraction)
    pct = int(fraction * 100)
    prefix = ""
    if member_total and member_total > 1 and member_index is not None:
        prefix = f"File {member_index}/{member_total} · "
    return f"{prefix}{_format_phase(phase)} {bar} {pct}%"


class SaveProgressReporter:
    """Bounded, deduplicating progress reporter for one transfer phase.

    Construction:
        reporter = SaveProgressReporter(edit_fn, phase="download", member_index=1, member_total=3)
        callback = reporter.callback()
        await client.download_media(msg, file=path, progress_callback=callback)

    The callback is a sync ``(received, total)`` callable suitable for Telethon.
    """

    def __init__(
        self,
        edit_fn: Callable[[str], Awaitable[bool]] | None,
        phase: str,
        *,
        member_index: int | None = None,
        member_total: int | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._edit_fn = edit_fn
        self._phase = phase
        self._member_index = member_index
        self._member_total = member_total
        self._loop = loop
        self._last_pct = -1
        self._last_render: str | None = None
        self._terminal = False

    def callback(self) -> Callable[[int, int], None]:
        milestones_cache: tuple[float, ...] | None = None
        reported: set[float] = set()

        def _on_progress(received: int, total: int) -> None:
            nonlocal milestones_cache, reported
            if self._terminal:
                return
            if total <= 0:
                if received > 0 and not reported:
                    self._edit_indeterminate()
                return
            if milestones_cache is None:
                milestones_cache = _milestones(total)
                reported = set()
            fraction = received / total if total > 0 else 0.0
            pct = int(fraction * 100)
            for milestone in milestones_cache:
                if fraction >= milestone and milestone not in reported:
                    reported.add(milestone)
                    self._edit_milestone(milestone)
                    break
            if pct == 100 and 1.0 not in reported:
                reported.add(1.0)
                self._edit_milestone(1.0)

        return _on_progress

    def mark_terminal(self) -> None:
        self._terminal = True

    def _edit_milestone(self, fraction: float) -> None:
        text = _render_status(
            self._phase, fraction, self._member_index, self._member_total
        )
        self._do_edit(text)

    def _edit_indeterminate(self) -> None:
        prefix = ""
        if self._member_total and self._member_total > 1 and self._member_index is not None:
            prefix = f"File {self._member_index}/{self._member_total} · "
        text = f"{prefix}{_format_phase(self._phase)} ▱▱▱▱▱ —"
        self._do_edit(text)

    def _do_edit(self, text: str) -> None:
        if text == self._last_render:
            return
        self._last_render = text
        if self._edit_fn is None:
            return
        loop = self._loop or asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(self._safe_edit(text), loop)
        else:
            loop.create_task(self._safe_edit(text))

    async def _safe_edit(self, text: str) -> None:
        if self._edit_fn is None:
            return
        try:
            await self._edit_fn(text)
        except Exception as exc:
            logger.warning("save progress edit failed: %s", exc)


def build_edit_fn(
    helper_client,
    chat_id: int,
    msg_id: int,
) -> Callable[[str], Awaitable[bool]] | None:
    """Build an async edit callable for the status message, or None.

    The edit reuses the helper bot's ``edit_message`` — the SAME in-place edit
    path every Glass UI panel uses. Returns ``None`` when no helper or no
    valid message target exists, so the reporter degrades to log-only.
    """
    if helper_client is None or not chat_id or not msg_id:
        return None

    async def _edit(text: str) -> bool:
        try:
            await helper_client.edit_message(chat_id, msg_id, text)
            return True
        except Exception as exc:
            logger.warning("save progress edit_message failed: %s", exc)
            return False

    return _edit
