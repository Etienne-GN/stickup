# stickup

Back up your computer to a USB stick by plugging it in.

stickup keeps an encrypted, incremental [restic](https://restic.net) backup on each
stick you enroll. Plug the stick in and the backup starts on its own; a notification
tells you when it is safe to unplug.

## Requirements

Debian or a derivative, with a GNOME session:

```
sudo apt install restic libsecret-tools python3-gi gir1.2-adw-1 python3-yaml udisks2 libnotify-bin
```

## Use

```
bin/stickup                 # open the window
bin/stickup devices         # list plugged-in USB filesystems
bin/stickup add <uuid>      # enroll a stick
bin/stickup status
bin/stickup backup <uuid>
bin/stickup snapshots <uuid>
bin/stickup restore <uuid> <path> <target-dir>
bin/stickup check-passphrase <uuid>
bin/stickup remove <uuid>
```

The stick must already be formatted and writable by you. Anything already on it is kept;
the backup lives in a `restic` folder.

## How it works

- Each stick has its own folders, exclusions and number of snapshots to keep, stored in
  `~/.config/stickup/config.yaml`.
- The passphrase lives in your keyring. You need to remember it to restore on another
  computer; it cannot be recovered.
- A systemd user unit tied to the stick's filesystem starts the backup when it appears.
  stickup mounts the stick itself, because Debian does not automount ext4 sticks, and it
  leaves the stick alone while the screen is locked.
- A run shorter than five minutes shows two notifications, started and done. A longer one
  also updates at five minutes and then at each ten percent.

## Restoring without stickup

The stick holds a plain restic repository:

```
restic -r /path/to/stick/restic snapshots
restic -r /path/to/stick/restic restore latest --target /some/dir
```

## Tests

```
python3 -m unittest discover -s tests
```

## Known limits

- Files only root can read are skipped and reported.
- Security software that watches for bulk file reads may stop restic until it is allowed.
