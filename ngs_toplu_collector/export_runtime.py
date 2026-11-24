"""Small, UI-independent progress hook for an isolated AI export job.

Ordinary callers need no callback.  The worker installs one that writes a
bounded status document; the Tk process only polls that document.
"""
from __future__ import annotations

from contextvars import ContextVar


_progress_callback = ContextVar('ai_export_progress_callback', default=None)


def set_progress_callback(callback):
    return _progress_callback.set(callback)


def reset_progress_callback(token):
    _progress_callback.reset(token)


def report_progress(stage, current=None, total=None, message=''):
    callback = _progress_callback.get()
    if callback is not None:
        callback({'stage': str(stage), 'current': current, 'total': total,
                  'message': str(message or '')})
