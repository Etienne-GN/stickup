"""Desktop notifications, kept rare because GNOME shows a banner on every update."""

import logging
import subprocess
import time

log = logging.getLogger(__name__)

QUIET_SECONDS = 300
LAST_STEP = 90


class Notifier:
    def __init__(self, title_prefix: str, enabled: bool = True):
        self.title_prefix = title_prefix
        self.enabled = enabled
        self.notification_id = "0"

    def send(self, title: str, body: str, critical: bool = False) -> None:
        if not self.enabled:
            return
        command = [
            "notify-send",
            "--app-name=stickup",
            "--icon=drive-removable-media",
            "--print-id",
            f"--replace-id={self.notification_id}",
        ]
        if critical:
            command.append("--urgency=critical")
        command += [f"{self.title_prefix}: {title}", body]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            log.warning("notify-send failed: %s", result.stderr.strip())
            return
        self.notification_id = result.stdout.strip() or self.notification_id


class ProgressSteps:
    """Decide when a running backup is worth one more notification.

    Nothing for the first QUIET_SECONDS; then the current percentage once, then
    each following multiple of ten up to LAST_STEP.
    """

    def __init__(self, quiet_seconds: int = QUIET_SECONDS, clock=time.monotonic):
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.started = clock()
        self.next_step: int | None = None

    def due(self, percent: int) -> int | None:
        if self.clock() - self.started < self.quiet_seconds:
            return None
        if self.next_step is None:
            shown = percent
        elif self.next_step <= LAST_STEP and percent >= self.next_step:
            shown = percent // 10 * 10
        else:
            return None
        self.next_step = (percent // 10 + 1) * 10
        return shown
