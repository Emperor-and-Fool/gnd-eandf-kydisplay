# Kydisplay

A KDE Plasma 6 cast app. Scaffolding in progress — full documentation will land here as the project takes shape.

A KDE Plasma 6 cast app. Scaffolding in progress — all documentation will land here as the project takes shape.

So far, after reading about Miracast I realized the stability of the stream on a 2,4 Ghz network will never be optimal. So it took some time tweaking Genome-Network-Display to find a sweet spot of quality and stability on a 2,4Ghz. With a video playing, that actually didn't really succeed unless your lucky and the router and your wifi-radio negotiate a connection that leaves you the headroom.

To achieve best as possible in the limited time we had, we added HW encoding on NVENC and iGPU to at least allow the render its own space to work.

So I realized I had to force 5Ghz on the Router if I wanted a better video format. We created a detector that will show you, you are on low bandwidth and allow you in one click to switch format. By the time that worked I realized the miracast would be best for presentations. Kwin did not (yet) us provide a method to get a smooth Virtual Screen. But a start has been made and it is on the ToDo list.

Since i want to watch video while I work,and most TV's these days have DLNA,we took some time to get that to work for most Peertube, Odysee, Rumble & YT video's. The Mirdispo Gui skeleton allowed us to do a quick build that shows when there is streamable video playing on your system. If the TV is found K-wi-display will stream it to any capable wireless display.

Let me know what you think.

## Built on GNOME Network Displays (and some work done by mirdispo creators)

The Miracast backend inherits its streaming engine from
[GNOME Network Displays](https://github.com/GNOME/gnome-network-displays),
maintained by Benjamin Berg and Christian Glombek, with Anupam Kumar,
Pedro Sader Azevedo and many other contributors. They have spent years on a
protocol that is unforgiving in practice.

Kydisplay's starting point for the KDE-side work and the patch stack was
[mirdispo](https://github.com/hencyber/mirdispo), which packaged the GND
engine without its GTK interface under a Plasma front end. I started working
on it at [Emperor-and-Fool/gnd-mirdispo-henc](https://github.com/Emperor-and-Fool/gnd-mirdispo-henc).
This repository redistributes that engine the same way, with the patches in
`packaging/patches/` — each one a fault found by using the engine rather
than a rewrite of it. The patches are written to be readable upstream, and
belong there more than here.

Both projects are under the GNU General Public License, version 3 or later.
The complete 0.99.0 source archive and the patches applied to it are in this
repository, so the exact source of every binary produced from it is
available. `packaging/copyright` records who holds copyright over what.

## Installing

Kydisplay needs a network connection during installation. The package is
small because it does not carry its Python dependencies — it fetches them at
install time into a private environment under /opt/kydisplay/venv.

This is deliberate. One of those dependencies is yt-dlp, which has to keep
pace with the sites it reads; a copy sealed into a .deb goes stale within
weeks and stream extraction starts failing. Fetching at install time means
you get the current one on the day you install.

If no network is available the package refuses to install rather than leaving
a half-configured one behind. Connect and try again.

```
sudo apt install ./kydisplay_<version>_amd64.deb
```

Everything else — ffmpeg, Qt, Python — comes from your distribution's own
packages and apt will pull them in.

## Keeping extraction working

When a site changes and a stream stops resolving, refresh the extractor
rather than waiting for a new release:

```
sudo kydisplay --update
```

## License

GPL-3.0-or-later. See `LICENSE` and `packaging/copyright`.