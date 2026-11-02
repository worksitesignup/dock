# Dock

A Tiger-style dock with OS X–style magnification for Wayland compositors (layer-shell), drawn with cairo/GTK3.

## Requirements
- Python 3, PyGObject, pycairo
- GTK 3, gtk-layer-shell (with GObject introspection)

Arch: `sudo pacman -S python-gobject python-cairo gtk3 gtk-layer-shell`

## Run
```sh
./run
```
Settings live in `config.json` (icon size, magnification, icon theme, sounds, volume).
