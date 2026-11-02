# Dock

A Tiger-style dock with OS X–style magnification for Wayland compositors (layer-shell), drawn with cairo/GTK3.

Built and tested on **KDE Plasma (Wayland)**. Other layer-shell compositors (Hyprland, Sway) should show the dock, but focusing already-open windows and the Meta-key search shortcut use KDE/KWin and won't work elsewhere.

## Requirements
- Python 3, PyGObject, pycairo
- GTK 3, gtk-layer-shell (with GObject introspection)

Arch: `sudo pacman -S python-gobject python-cairo gtk3 gtk-layer-shell`

## Features
- Apps grouped into categories; hover for a bubble, pause or click to open it
- Type-to-search (tap Meta)
- Folder stacks next to the Trash: the bubble shows the folder's files, newest first; scroll to page through big folders
- Drag folders from your file manager onto the dock to add them; drag them off (from the shelf or the open bubble) to remove, or along the shelf to reorder
- Mounted drives (USB, SD, network shares) appear automatically; right-click to eject
- Trash shows its contents and full/empty state

## Run
```sh
./run
```
The default icon theme is `oxygen`. Change `icon_theme` if you don't have it installed. Settings live in `config.json` (folders, mounts, icon size, magnification, icon theme, sounds, volume).
