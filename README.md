# Aurora Updater

Aurora Updater distributes Aurora-managed components from this repository to another Aurora installation. It is intentionally separate from Void XBPS system updates and Flatpak updates.

## What it updates

The manifest can deliver Aurora Settings, the AI manager, keyboard shortcuts, user icons, wallpapers, GTK/Qt theme assets, Noctalia plugins, fonts, and other allowlisted user files. Each file has a SHA-256 digest. Files are downloaded into a staging directory, verified, then atomically installed. Existing files are copied to `~/.local/state/aurora-updater/backups/<timestamp>` before replacement.

`required_packages` is only for explicit Aurora dependencies. It does not run a general system upgrade. Package installation uses `pkexec xbps-install` and never stores a password. Kernel and hardware changes should be represented by reviewed package entries or handled by the separate Kernel & Drivers page.

## Publishing a release

1. Put new Aurora-managed files under `payload/`.
2. Update the manifest version, release notes, targets and SHA-256 values.
3. Commit and push to the `main` branch.
4. On another Aurora installation open **Systeeminfo → Aurora Updates** and press **Controleren**. The user sees the release notes and a live progress bar before installing.

The client accepts only these user-owned targets: `~/.local/bin`, `~/.config/aurora`, `~/.config/autostart`, `~/.local/share/applications`, `~/.local/share/icons`, `~/.local/share/fonts`, and GTK configuration directories. Repository scripts are never executed.

## License and attribution

Original Aurora materials are copyright © 2026 Hrwoje Dabo, media4now.nl, and
are provided under the **Aurora Proprietary License v1.0** in [`LICENSE`](LICENSE).
The updater installs a copy at `~/.local/share/doc/aurora/AURORA-LICENSE.txt` so
the license travels with the Aurora installation. Void, Niri, Noctalia, GTK,
Flatpak and all other third-party components remain under their own licenses.
The Aurora name, logo and branding require separate permission for any use that
suggests official endorsement.

## Release versions and repeat-safe installation

Every release must use a unique `version` and a human-readable `release_name` in
`manifest.json`. Aurora Settings shows both values and the installation time.
The updater stores the active release and the last 20 installed releases in
`~/.local/state/aurora-updater/installed.json` and `history.json`. If the same
version and verified payloads are already installed, a second click is reported
as already installed and no files, packages, or system actions are run again.
