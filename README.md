# Dock

A Tiger-style dock with OS X–style magnification for Wayland compositors (layer-shell), drawn with cairo/GTK3.

Built and tested on **KDE Plasma (Wayland)**. Other layer-shell compositors (Hyprland, Sway) should show the dock, but focusing already-open windows and the Meta-key search shortcut use KDE/KWin and won't work elsewhere.

## Requirements
- Python 3, PyGObject, pycairo
- GTK 3, gtk-layer-shell (with GObject introspection)

Arch: `sudo pacman -S python-gobject python-cairo gtk3 gtk-layer-shell`

## Run
```sh
./run
```
The default icon theme is `oxygen`. Change `icon_theme` if you don't have it installed. Settings live in `config.json` (icon size, magnification, icon theme, sounds, volume).
