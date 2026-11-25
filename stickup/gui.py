"""The stickup window: which stick backs up what, and the buttons to act on it."""

import subprocess
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import backup, config, devices, units  # noqa: E402

APP_ID = "io.github.stickup"
REFRESH_SECONDS = 2
STALE_DAYS = 7

CSS = """
.stickup-hero { padding: 18px 6px 6px 6px; }
.stickup-hero-title { font-size: 1.7em; font-weight: 800; }
.stickup-pill { padding: 3px 12px; border-radius: 999px; font-weight: 700; font-size: 0.85em; }
.stickup-pill.ok { background: alpha(@success_color, 0.18); color: @success_color; }
.stickup-pill.warn { background: alpha(@warning_color, 0.2); color: @warning_color; }
.stickup-pill.bad { background: alpha(@error_color, 0.18); color: @error_color; }
.stickup-pill.idle { background: alpha(currentColor, 0.1); }
.stickup-meter trough, .stickup-meter progress { min-height: 10px; border-radius: 999px; }
"""


def human_bytes(count: float | None) -> str:
    if not count:
        return "0"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{count:.1f} {unit}"
        count /= 1024
    return ""


def human_age(timestamp: str | None) -> tuple[str, int | None]:
    if not timestamp:
        return "never", None
    elapsed = datetime.now() - datetime.fromisoformat(timestamp)
    days = elapsed.days
    if days >= 1:
        return ("yesterday" if days == 1 else f"{days} days ago"), days
    hours = elapsed.seconds // 3600
    if hours >= 1:
        return f"{hours} h ago", 0
    minutes = elapsed.seconds // 60
    return ("just now" if minutes < 2 else f"{minutes} min ago"), 0


def in_thread(work, done):
    """Run work() off the main loop, then done(result, error) back on it."""

    def runner():
        try:
            result, error = work(), None
        except Exception as caught:  # surfaced to the user by done()
            result, error = None, caught
        GLib.idle_add(done, result, error)

    threading.Thread(target=runner, daemon=True).start()


def describe(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or str(error)).strip()
    return str(error).strip("'\"")


class PathList(Adw.PreferencesGroup):
    """An editable list of paths with a remove button per row and an add button."""

    def __init__(self, title: str, description: str, on_change, pick_folders: bool = True):
        super().__init__(title=title, description=description)
        self.on_change = on_change
        self.pick_folders = pick_folders
        self.paths: list[str] = []
        self.rows: list[Gtk.Widget] = []

        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Add a folder")
        add.add_css_class("flat")
        add.connect("clicked", self.choose)
        self.set_header_suffix(add)

    def set_paths(self, paths: list[str]) -> None:
        self.paths = list(paths)
        for row in self.rows:
            self.remove(row)
        self.rows = []
        for path in self.paths:
            row = Adw.ActionRow(title=GLib.markup_escape_text(path))
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            remove = Gtk.Button(icon_name="edit-delete-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove")
            remove.add_css_class("flat")
            remove.connect("clicked", self.drop, path)
            row.add_suffix(remove)
            self.add(row)
            self.rows.append(row)
        if not self.paths:
            row = Adw.ActionRow(title="Nothing here yet", sensitive=False)
            self.add(row)
            self.rows.append(row)

    def drop(self, _button, path: str) -> None:
        self.on_change([kept for kept in self.paths if kept != path])

    def choose(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Choose a folder")
        dialog.select_folder(self.get_root(), None, self.chosen)

    def chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        home = GLib.get_home_dir()
        if path == home:
            path = "~"
        elif path.startswith(home + "/"):
            path = "~" + path[len(home):]
        if path not in self.paths:
            self.on_change([*self.paths, path])


class StickPage(Adw.Bin):
    def __init__(self, window: "Window"):
        super().__init__()
        self.window = window
        self.stick: config.Stick | None = None
        self.loading = False

        self.title = Gtk.Label(xalign=0, css_classes=["stickup-hero-title"])
        self.subtitle = Gtk.Label(xalign=0, css_classes=["dim-label"])
        self.pill = Gtk.Label(valign=Gtk.Align.CENTER, css_classes=["stickup-pill"])
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        names.append(self.title)
        names.append(self.subtitle)
        hero = Gtk.Box(spacing=12, css_classes=["stickup-hero"])
        hero.append(Gtk.Image(icon_name="drive-removable-media-symbolic", pixel_size=48))
        hero.append(names)
        hero.append(self.pill)

        self.progress = Gtk.ProgressBar(css_classes=["stickup-meter"])
        self.progress_label = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.progress_box.append(self.progress)
        self.progress_box.append(self.progress_label)

        self.backup_button = Gtk.Button(label="Back up now", css_classes=["suggested-action", "pill"])
        self.backup_button.connect("clicked", self.back_up_now)
        self.restore_button = Gtk.Button(label="Restore…", css_classes=["pill"])
        self.restore_button.connect("clicked", lambda _b: RestoreDialog(self.window, self.stick).present(self.window))
        self.check_button = Gtk.Button(label="Test passphrase", css_classes=["pill"])
        self.check_button.connect("clicked", lambda _b: PassphraseDialog(self.window, self.stick).present(self.window))
        actions = Gtk.Box(spacing=10, halign=Gtk.Align.START)
        for button in (self.backup_button, self.restore_button, self.check_button):
            actions.append(button)

        self.last_row = Adw.ActionRow(title="Last backup")
        self.space_row = Adw.ActionRow(title="Space on the stick")
        self.space_meter = Gtk.LevelBar(valign=Gtk.Align.CENTER, width_request=140)
        self.space_row.add_suffix(self.space_meter)
        self.snapshots_row = Adw.ActionRow(title="Snapshots kept")
        overview = Adw.PreferencesGroup(title="Overview")
        for row in (self.last_row, self.space_row, self.snapshots_row):
            row.add_css_class("property")
            overview.add(row)

        self.sources = PathList("What is backed up", "Folders copied to this stick.", self.sources_changed)
        self.excludes = PathList("Left out", "Folders inside the above that are skipped.", self.excludes_changed)

        self.auto_row = Adw.SwitchRow(title="Back up when plugged in", subtitle="Starts by itself, then unmounts the stick")
        self.auto_row.connect("notify::active", self.option_changed)
        self.caches_row = Adw.SwitchRow(title="Skip cache folders", subtitle="Build output and caches that mark themselves as such")
        self.caches_row.connect("notify::active", self.option_changed)
        self.locked_row = Adw.SwitchRow(title="Wait for an unlocked screen", subtitle="Never mount the stick while the session is locked")
        self.locked_row.connect("notify::active", self.option_changed)
        self.keep_row = Adw.SpinRow.new_with_range(1, 100, 1)
        self.keep_row.set_title("Snapshots to keep")
        self.keep_row.set_subtitle("Older ones are removed after each backup")
        self.keep_row.connect("notify::value", self.option_changed)
        options = Adw.PreferencesGroup(title="Options")
        for row in (self.auto_row, self.keep_row, self.caches_row, self.locked_row):
            options.add(row)

        forget = Adw.ButtonRow(title="Forget this stick")
        forget.add_css_class("destructive-action")
        forget.connect("activated", self.forget)
        danger = Adw.PreferencesGroup(description="Forgetting a stick stops backups to it. The data on it is not touched.")
        danger.add(forget)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=12, margin_bottom=32)
        for widget in (hero, self.progress_box, actions, overview, self.sources, self.excludes, options, danger):
            column.append(widget)
        clamp = Adw.Clamp(maximum_size=640, child=column, margin_start=18, margin_end=18)
        self.set_child(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER))

    def show_stick(self, stick: config.Stick) -> None:
        self.stick = stick
        self.loading = True
        self.title.set_label(stick.name)
        self.sources.set_paths(stick.sources)
        self.excludes.set_paths(stick.excludes)
        self.auto_row.set_active(stick.auto)
        self.caches_row.set_active(stick.exclude_caches)
        self.locked_row.set_active(stick.skip_when_locked)
        self.keep_row.set_value(stick.keep_last)
        self.loading = False
        self.refresh()

    def refresh(self) -> None:
        stick = self.stick
        if stick is None:
            return
        filesystem = devices.find(stick.uuid)
        running = units.is_running(stick.uuid)
        last = config.read_last_run(stick.uuid)
        age, days = human_age(last.get("finished"))

        if filesystem:
            self.subtitle.set_label(f"{filesystem.drive}  ·  {filesystem.size}  ·  plugged in")
        else:
            self.subtitle.set_label("Not plugged in")

        if running:
            self.set_pill("Backing up", "warn")
        elif last.get("status") == "failed":
            self.set_pill("Last run failed", "bad")
        elif days is None:
            self.set_pill("Never backed up", "warn")
        elif days >= STALE_DAYS:
            self.set_pill(f"{days} days old", "warn")
        elif last.get("status") == "incomplete":
            self.set_pill("Some files skipped", "warn")
        else:
            self.set_pill("Up to date", "ok")

        progress = config.read_progress(stick.uuid) if running else {}
        self.progress_box.set_visible(running)
        if running:
            percent = progress.get("percent", 0)
            self.progress.set_fraction(percent / 100)
            self.progress_label.set_label(f"{progress.get('phase', 'starting').capitalize()}  ·  {percent}%")

        self.backup_button.set_sensitive(bool(filesystem) and not running)
        self.restore_button.set_sensitive(bool(filesystem) and not running)
        self.check_button.set_sensitive(bool(filesystem) and not running)

        if last.get("status") == "failed":
            self.last_row.set_subtitle(GLib.markup_escape_text(f"Failed: {last.get('error', 'unknown error')}"))
        elif last:
            took = backup.human_duration(last.get("duration_seconds", 0))
            self.last_row.set_subtitle(f"{age}  ·  {human_bytes(last.get('bytes'))} in {took}")
        else:
            self.last_row.set_subtitle("Never")

        if last.get("stick_total"):
            used, total = last["stick_used"], last["stick_total"]
            self.space_row.set_subtitle(f"{human_bytes(used)} used of {human_bytes(total)}")
            self.space_meter.set_value(used / total)
            self.space_meter.set_visible(True)
        else:
            self.space_row.set_subtitle("Unknown until the first backup")
            self.space_meter.set_visible(False)
        count = last.get("snapshot_count")
        self.snapshots_row.set_subtitle(f"{count} of {stick.keep_last}" if count else "None yet")

    def set_pill(self, text: str, kind: str) -> None:
        self.pill.set_label(text)
        for name in ("ok", "warn", "bad", "idle"):
            self.pill.remove_css_class(name)
        self.pill.add_css_class(kind)

    def save(self) -> None:
        self.window.configuration.save()
        self.refresh()

    def sources_changed(self, paths: list[str]) -> None:
        self.stick.sources = paths
        self.sources.set_paths(paths)
        self.save()

    def excludes_changed(self, paths: list[str]) -> None:
        self.stick.excludes = paths
        self.excludes.set_paths(paths)
        self.save()

    def option_changed(self, *_arguments) -> None:
        if self.loading or self.stick is None:
            return
        self.stick.auto = self.auto_row.get_active()
        self.stick.exclude_caches = self.caches_row.get_active()
        self.stick.skip_when_locked = self.locked_row.get_active()
        self.stick.keep_last = int(self.keep_row.get_value())
        if self.stick.auto:
            units.enable(self.stick.uuid)
        else:
            units.disable(self.stick.uuid)
        self.save()

    def back_up_now(self, _button) -> None:
        try:
            units.start(self.stick.uuid)
        except subprocess.CalledProcessError as error:
            self.window.toast(f"Could not start: {describe(error)}")
            return
        self.window.toast("Backup started")
        self.refresh()

    def forget(self, _row) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Forget {self.stick.name}?",
            body="Backups to this stick stop. The data already on it is not touched.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self.forget_answered)
        dialog.present(self.window)

    def forget_answered(self, _dialog, response: str) -> None:
        if response != "forget":
            return
        units.disable(self.stick.uuid)
        del self.window.configuration.sticks[self.stick.uuid]
        self.window.configuration.save()
        self.window.reload()


class AddDialog(Adw.Dialog):
    def __init__(self, window: "Window", filesystem: devices.Filesystem):
        super().__init__(title="Add a stick", content_width=440)
        self.window = window
        self.filesystem = filesystem

        self.name_row = Adw.EntryRow(title="Name", text=filesystem.label or filesystem.drive or "USB stick")
        self.first = Adw.PasswordEntryRow(title="Backup passphrase")
        self.second = Adw.PasswordEntryRow(title="Repeat the passphrase")
        for row in (self.name_row, self.first, self.second):
            row.connect("changed", self.validate)

        group = Adw.PreferencesGroup(
            title=GLib.markup_escape_text(f"{filesystem.drive or 'USB stick'}  ·  {filesystem.size}"),
            description="The backup is encrypted with this passphrase. It is kept in your keyring so "
            "backups run by themselves, but you need to remember it to restore on another computer. "
            "There is no way to recover it.",
        )
        for row in (self.name_row, self.first, self.second):
            group.add(row)

        self.status = Gtk.Label(wrap=True, xalign=0, css_classes=["error"], visible=False)
        self.add_button = Gtk.Button(label="Add stick", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER, sensitive=False)
        self.add_button.connect("clicked", self.enroll)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12, margin_bottom=24, margin_start=18, margin_end=18)
        for widget in (group, self.status, self.add_button):
            box.append(widget)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)

    def validate(self, *_arguments) -> None:
        first, second = self.first.get_text(), self.second.get_text()
        self.add_button.set_sensitive(bool(self.name_row.get_text().strip()) and len(first) >= 8 and first == second)

    def enroll(self, _button) -> None:
        stick = config.Stick(uuid=self.filesystem.uuid, name=self.name_row.get_text().strip())
        passphrase = self.first.get_text()
        self.add_button.set_sensitive(False)
        self.add_button.set_label("Preparing the stick…")
        in_thread(lambda: backup.enroll(stick, passphrase), lambda existing, error: self.enrolled(stick, existing, error))

    def enrolled(self, stick: config.Stick, existing: bool, error) -> None:
        if error:
            self.status.set_label(describe(error))
            self.status.set_visible(True)
            self.add_button.set_label("Add stick")
            self.validate()
            return
        self.window.configuration.sticks[stick.uuid] = stick
        self.window.configuration.save()
        units.enable(stick.uuid)
        self.window.reload(select=stick.uuid)
        if existing:
            self.window.toast(f"{stick.name} added. It already held a backup, which carries on from here.")
        else:
            self.window.toast(f"{stick.name} added. Plug it in any time to back up.")
        self.close()


class PassphraseDialog(Adw.Dialog):
    def __init__(self, window: "Window", stick: config.Stick):
        super().__init__(title="Test passphrase", content_width=400)
        self.window = window
        self.stick = stick
        self.entry = Adw.PasswordEntryRow(title="Backup passphrase")
        self.entry.connect("entry-activated", self.check)
        group = Adw.PreferencesGroup(description="Type it from memory. This checks it against the stick, not the keyring.")
        group.add(self.entry)
        self.result = Gtk.Label(wrap=True)
        self.button = Gtk.Button(label="Check", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.button.connect("clicked", self.check)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12, margin_bottom=24, margin_start=18, margin_end=18)
        for widget in (group, self.result, self.button):
            box.append(widget)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)

    def check(self, *_arguments) -> None:
        typed = self.entry.get_text()
        self.button.set_sensitive(False)
        self.result.set_label("Checking…")
        in_thread(lambda: backup.passphrase_opens(self.stick, typed), self.checked)

    def checked(self, opens, error) -> None:
        if error:
            heading, body = "Could not check the passphrase", describe(error)
        elif opens:
            heading, body = "Passphrase correct", f"This passphrase opens the backup on {self.stick.name}."
        else:
            heading, body = "Wrong passphrase", f"That is not the passphrase for {self.stick.name}."
        self.close()
        alert = Adw.AlertDialog(heading=heading, body=body)
        alert.add_response("ok", "OK")
        alert.present(self.window)


class RestoreDialog(Adw.Dialog):
    def __init__(self, window: "Window", stick: config.Stick):
        super().__init__(title="Restore", content_width=560, content_height=680)
        self.window = window
        self.stick = stick
        self.snapshots: list[dict] = []
        self.target: str | None = None
        self.directory = "/"
        self.busy = True

        self.snapshot_row = Adw.ComboRow(title="From the backup of", model=Gtk.StringList.new(["Reading the stick…"]))
        self.snapshot_row.connect("notify::selected", self.snapshot_changed)
        snapshot_group = Adw.PreferencesGroup()
        snapshot_group.add(self.snapshot_row)

        self.up_button = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Parent folder", css_classes=["flat"])
        self.up_button.connect("clicked", lambda _b: self.load(self.directory.rsplit("/", 1)[0] or "/"))
        self.location = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.START, css_classes=["heading"])
        self.spinner = Gtk.Spinner()
        location_bar = Gtk.Box(spacing=6)
        for widget in (self.up_button, self.location, self.spinner):
            location_bar.append(widget)

        self.entries = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False)
        self.entries.connect("row-selected", lambda *_a: self.update_button())
        self.entries.connect("row-activated", self.entry_activated)
        scroller = Gtk.ScrolledWindow(child=self.entries, vexpand=True, min_content_height=260, css_classes=["card"])
        scroller.set_overflow(Gtk.Overflow.HIDDEN)
        hint = Gtk.Label(
            label="Click a file or folder to select it. Double-click a folder to open it.",
            xalign=0,
            wrap=True,
            css_classes=["dim-label", "caption"],
        )

        self.target_row = Adw.ActionRow(title="Put the copy in", subtitle="Choose a folder", activatable=True)
        self.target_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        self.target_row.connect("activated", self.choose_target)
        target_group = Adw.PreferencesGroup(description="Nothing is overwritten in place: the copy lands inside the folder you choose, under its original path.")
        target_group.add(self.target_row)

        self.status = Gtk.Label(wrap=True, xalign=0, visible=False)
        self.button = Gtk.Button(label="Restore", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER, sensitive=False)
        self.button.connect("clicked", self.restore)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=24, margin_start=18, margin_end=18)
        for widget in (snapshot_group, location_bar, scroller, hint, target_group, self.status, self.button):
            box.append(widget)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.connect("closed", self.closed)
        self.set_busy(True)
        in_thread(lambda: backup.list_snapshots(stick, keep_mounted=True), self.snapshots_loaded)

    def closed(self, _dialog) -> None:
        in_thread(lambda: backup.release(self.stick), lambda _unmounted, _error: False)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.spinner.set_spinning(busy)
        for widget in (self.entries, self.snapshot_row):
            widget.set_sensitive(not busy)
        self.up_button.set_sensitive(not busy and self.directory != "/")
        self.update_button()

    def snapshots_loaded(self, snapshots, error) -> None:
        if error:
            self.spinner.set_spinning(False)
            self.say(describe(error), "error")
            return
        self.snapshots = list(reversed(snapshots))
        labels = [snapshot["time"][:16].replace("T", "  ") for snapshot in self.snapshots]
        self.snapshot_row.set_model(Gtk.StringList.new(labels or ["No backup on this stick yet"]))
        if not self.snapshots:
            self.spinner.set_spinning(False)
            return
        home = GLib.get_home_dir()
        self.load(home if home in self.snapshots[0].get("paths", []) else "/")

    def snapshot_changed(self, *_arguments) -> None:
        if self.snapshots and not self.busy:
            self.load(self.directory)

    def selected_snapshot(self) -> str:
        return self.snapshots[self.snapshot_row.get_selected()]["short_id"]

    def load(self, directory: str) -> None:
        snapshot = self.selected_snapshot()
        self.set_busy(True)
        in_thread(
            lambda: backup.list_directory(self.stick, snapshot, directory),
            lambda entries, error: self.listed(directory, entries, error),
        )

    def listed(self, directory: str, entries, error) -> None:
        if error:
            self.set_busy(False)
            self.say(describe(error), "error")
            return
        self.directory = directory
        self.location.set_label(directory)
        self.status.set_visible(False)
        self.entries.remove_all()
        for entry in entries:
            self.entries.append(self.entry_row(entry))
        if not entries:
            self.entries.append(Gtk.ListBoxRow(child=Gtk.Label(label="Empty folder", margin_top=12, margin_bottom=12), sensitive=False))
        self.set_busy(False)

    def entry_row(self, entry: dict) -> Gtk.ListBoxRow:
        is_folder = entry.get("type") == "dir"
        line = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        line.append(Gtk.Image(icon_name="folder-symbolic" if is_folder else "text-x-generic-symbolic"))
        line.append(Gtk.Label(label=str(entry.get("name", "")), xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE))
        if is_folder:
            line.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        else:
            line.append(Gtk.Label(label=human_bytes(entry.get("size")), css_classes=["dim-label", "caption"]))
        row = Gtk.ListBoxRow(child=line)
        row.entry = entry
        return row

    def entry_activated(self, _list, row) -> None:
        entry = getattr(row, "entry", None)
        if entry and entry.get("type") == "dir" and not self.busy:
            self.load(entry["path"])

    def chosen_path(self) -> str:
        row = self.entries.get_selected_row()
        entry = getattr(row, "entry", None)
        return entry["path"] if entry else self.directory

    def choose_target(self, _row) -> None:
        Gtk.FileDialog(title="Put the copy in").select_folder(self.window, None, self.target_chosen)

    def target_chosen(self, dialog, result) -> None:
        try:
            self.target = dialog.select_folder_finish(result).get_path()
        except GLib.Error:
            return
        self.target_row.set_subtitle(GLib.markup_escape_text(self.target))
        self.update_button()

    def update_button(self) -> None:
        chosen = self.chosen_path()
        name = chosen.rsplit("/", 1)[1] or "everything"
        self.button.set_label(f"Restore {name}" if self.snapshots else "Restore")
        self.button.set_sensitive(bool(self.snapshots and self.target and not self.busy))

    def say(self, text: str, kind: str | None = None) -> None:
        for name in ("success", "error"):
            self.status.remove_css_class(name)
        if kind:
            self.status.add_css_class(kind)
        self.status.set_label(text)
        self.status.set_visible(True)

    def restore(self, _button) -> None:
        snapshot, path, target = self.selected_snapshot(), self.chosen_path(), self.target
        self.set_busy(True)
        self.say("Restoring…")
        in_thread(
            lambda: backup.restore(self.stick, path, target, snapshot, keep_mounted=True),
            lambda _r, error: self.restored(path, target, error),
        )

    def restored(self, path: str, target: str, error) -> None:
        self.set_busy(False)
        if error:
            self.say(describe(error), "error")
        else:
            self.say(f"Done. Your copy is in {target}{path}", "success")


class Window(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title="stickup", default_width=920, default_height=680)
        self.configuration = config.Config()
        self.rows: dict[str, Gtk.ListBoxRow] = {}

        self.sidebar_list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar_list.connect("row-selected", self.row_selected)
        sidebar_view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.sidebar_list, hscrollbar_policy=Gtk.PolicyType.NEVER))
        sidebar_view.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title="stickup")))

        self.page = StickPage(self)
        self.empty = Adw.StatusPage(
            icon_name="drive-removable-media-symbolic",
            title="Plug in a USB stick",
            description="Pick it on the left to turn it into a backup. After that, plugging it in is all it takes.",
        )
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.page, "stick")
        content_view = Adw.ToolbarView(content=self.stack)
        content_view.add_top_bar(Adw.HeaderBar(show_title=False))

        split = Adw.NavigationSplitView(min_sidebar_width=250, max_sidebar_width=300)
        split.set_sidebar(Adw.NavigationPage(title="Sticks", child=sidebar_view))
        split.set_content(Adw.NavigationPage(title="Backup", child=content_view))
        self.toasts = Adw.ToastOverlay(child=split)
        self.set_content(self.toasts)

        self.signature = None
        self.reload()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.tick)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, use_markup=False))

    def plugged_signature(self):
        plugged = tuple(sorted(filesystem.uuid for filesystem in devices.usb_filesystems()))
        running = tuple(uuid for uuid in self.configuration.sticks if units.is_running(uuid))
        return plugged, running

    def tick(self) -> bool:
        edited_elsewhere = self.configuration.changed_on_disk()
        if edited_elsewhere:
            self.configuration.load()
        signature = self.plugged_signature()
        if edited_elsewhere or signature != self.signature:
            selected = self.selected_uuid()
            self.reload(select=selected)
        elif self.stack.get_visible_child_name() == "stick":
            self.page.refresh()
        return True

    def selected_uuid(self) -> str | None:
        row = self.sidebar_list.get_selected_row()
        return getattr(row, "uuid", None) if row else None

    def reload(self, select: str | None = None) -> None:
        self.signature = self.plugged_signature()
        self.sidebar_list.remove_all()
        self.rows = {}
        plugged = {filesystem.uuid: filesystem for filesystem in devices.usb_filesystems()}

        for stick in self.configuration.sticks.values():
            last = config.read_last_run(stick.uuid)
            age, _days = human_age(last.get("finished"))
            where = "plugged in" if stick.uuid in plugged else "not plugged in"
            self.add_row(stick.uuid, stick.name, f"Last backup {age}  ·  {where}", enrolled=True)
        for filesystem in plugged.values():
            if filesystem.uuid in self.configuration.sticks:
                continue
            name = filesystem.label or filesystem.drive or "USB stick"
            self.add_row(filesystem.uuid, name, f"{filesystem.size}  ·  not set up", enrolled=False, filesystem=filesystem)

        target = self.rows.get(select) or next((row for row in self.rows.values() if row.enrolled), None)
        if target:
            self.sidebar_list.select_row(target)
        else:
            self.empty.set_title("Plug in a USB stick")
            self.empty.set_description(
                "Pick it on the left to turn it into a backup. After that, plugging it in is all it takes."
            )
            self.empty.set_child(None)
            self.stack.set_visible_child_name("empty")

    def add_row(self, uuid: str, title: str, subtitle: str, enrolled: bool, filesystem=None) -> None:
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
        icon = "drive-removable-media-symbolic" if enrolled else "list-add-symbolic"
        row.add_prefix(Gtk.Image(icon_name=icon))
        row.uuid, row.enrolled, row.filesystem = uuid, enrolled, filesystem
        self.sidebar_list.append(row)
        self.rows[uuid] = row

    def row_selected(self, _list, row) -> None:
        if row is None:
            return
        if row.enrolled:
            self.page.show_stick(self.configuration.sticks[row.uuid])
            self.stack.set_visible_child_name("stick")
        else:
            self.empty.set_title(row.get_title())
            self.empty.set_description("This stick is not a backup yet. Anything already on it is kept.")
            button = Gtk.Button(label="Set up as backup", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _b: AddDialog(self, row.filesystem).present(self))
            self.empty.set_child(button)
            self.stack.set_visible_child_name("empty")


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self):
        window = self.get_active_window() or Window(self)
        window.present()


def main() -> int:
    return Application().run(None)
