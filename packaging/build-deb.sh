#!/bin/bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
version="3.8.0"
architecture="amd64"
package_name="kydisplay"
output_dir="${project_root}/outputs"
package_root="$(mktemp -d -t knd-package-XXXXXX)"

# Archives record when each file was last written, which is a record of when
# somebody sat at this machine. The release date from the AppStream metadata
# is used instead: it is already public, and it makes the archive the same
# whoever builds it.
release_date="$(sed -n 's/.*<release version="[^"]*" date="\([0-9-]*\)".*/\1/p' \
    "${project_root}/nl.eandf.Kydisplay.metainfo.xml" | head -n 1)"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(date -u -d "${release_date} 00:00:00" +%s)}"


cleanup() {
    case "${package_root}" in
        /tmp/knd-package-*) rm -rf "${package_root}" ;;
    esac
}
trap cleanup EXIT

install -d \
    "${package_root}/DEBIAN" \
    "${package_root}/usr/bin" \
    "${package_root}/usr/lib/kydisplay" \
    "${package_root}/usr/libexec" \
    "${package_root}/usr/share/applications" \
    "${package_root}/usr/share/icons" \
    "${package_root}/usr/share/metainfo" \
    "${package_root}/usr/share/doc/${package_name}" \
    "${output_dir}"

cp -R "${project_root}/src/kydisplay" \
    "${package_root}/usr/lib/kydisplay/"
find "${package_root}/usr/lib/kydisplay" -type d -name __pycache__ -prune -exec rm -rf {} +
find "${package_root}/usr/lib/kydisplay" -type f -exec chmod 0644 {} +

install -m 0755 "${project_root}/bin/kydisplay" \
    "${package_root}/usr/bin/kydisplay"
install -m 0755 "${project_root}/vendor/amd64/kydisplay-daemon" \
    "${package_root}/usr/libexec/kydisplay-daemon"
install -m 0755 "${project_root}/vendor/amd64/gnome-network-displays-stream" \
    "${package_root}/usr/libexec/gnome-network-displays-stream"
install -m 0644 "${project_root}/nl.eandf.Kydisplay.desktop" \
    "${package_root}/usr/share/applications/nl.eandf.Kydisplay.desktop"
install -m 0644 "${project_root}/nl.eandf.Kydisplay.metainfo.xml" \
    "${package_root}/usr/share/metainfo/nl.eandf.Kydisplay.metainfo.xml"
install -m 0644 "${project_root}/README.md" "${package_root}/usr/share/doc/${package_name}/README.md"
install -m 0644 "${project_root}/packaging/copyright" "${package_root}/usr/share/doc/${package_name}/copyright"
install -m 0644 "${project_root}/LICENSE" "${package_root}/usr/share/doc/${package_name}/LICENSE"
install -m 0644 "${project_root}/vendor/source/gnome-network-displays-0.99.0.tar.xz" \
    "${package_root}/usr/share/doc/${package_name}/gnome-network-displays-0.99.0.tar.xz"
for patch in "${project_root}"/packaging/patches/*.patch; do
    install -m 0644 "${patch}" "${package_root}/usr/share/doc/${package_name}/$(basename "${patch}")"
done

cp -R "${project_root}/assets/icons/hicolor" "${package_root}/usr/share/icons/"
find "${package_root}/usr/share/icons" -type d -exec chmod 0755 {} +
find "${package_root}/usr/share/icons" -type f -exec chmod 0644 {} +

cat >"${package_root}/DEBIAN/control" <<EOF
Package: ${package_name}
Version: ${version}
Section: kde
Priority: optional
Architecture: ${architecture}
Maintainer: Kydisplay contributors <thomas.kai.tchong@emperorconsultancy.nl>
Depends: python3 (>= 3.10), python3-venv, python3-pip, ca-certificates, ffmpeg, python3-pyqt6, python3-pyqt6.qtqml, python3-dbus, python3-gi, gir1.2-gupnpav-1.0, qml6-module-org-kde-kirigami, qml6-module-org-kde-desktop, network-manager, wpasupplicant | iwd, xdg-desktop-portal, xdg-desktop-portal-kde, krfb, libavahi-common3 (>= 0.6.16), libavahi-gobject0 (>= 0.6.22), libc6 (>= 2.34), libglib2.0-0t64 (>= 2.83.0), libgstreamer-plugins-base1.0-0 (>= 1.6.0), libgstreamer1.0-0 (>= 1.6.0), libgstrtspserver-1.0-0 (>= 1.8.0), libjson-glib-1.0-0 (>= 1.5.2), libnm0 (>= 1.40.4), libportal1 (>= 0.7), libprotobuf-c1 (>= 1.0.1), libpulse0 (>= 0.99.1), libsoup-3.0-0 (>= 3.0.3), gstreamer1.0-pipewire, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly
Recommends: gstreamer1.0-libav, gstreamer1.0-pulseaudio
Conflicts: gnome-network-displays, kde-network-displays
Replaces: kde-network-displays
Homepage: https://git.redpie.nl/EmperorAndFoolICT-Mediaproductie/gnd-kydisplay-eandf
Description: Plasma-native wireless display casting
 Discover and cast the Plasma desktop to Miracast and Chromecast receivers.
 The application uses Qt 6 and Kirigami and includes an open-source WFD
 streaming backend derived from GNOME Network Displays 0.99.0.
EOF

cat >"${package_root}/DEBIAN/preinst" <<'EOF'
#!/bin/sh
# Refuse rather than half-install.
#
# Configuring this package fetches yt-dlp from the network. If that
# cannot be reached the fetch fails in postinst, and a postinst failure
# leaves the package unpacked but unconfigured — installed enough to
# look installed, broken enough not to run. Checking here instead means
# nothing is unpacked at all and "apt install" simply declines.
#
# getent rather than curl or wget: it comes from libc, so it is present
# before any dependency of this package has been configured, which is
# more than can be said for anything else that could do the asking.
set -e
case "$1" in
    install|upgrade)
        # An environment that already exists does not need fetching
        # again, so an upgrade on a disconnected machine is fine.
        if [ -x /opt/kydisplay/venv/bin/python3 ]; then
            exit 0
        fi
        if ! getent hosts pypi.org >/dev/null 2>&1; then
            echo "kydisplay: installation needs a network connection." >&2
            echo "  The package does not carry its stream extractor: yt-dlp" >&2
            echo "  has to keep pace with the sites it reads, so it is" >&2
            echo "  fetched at install time and is current on the day you" >&2
            echo "  install. Connect and try again." >&2
            exit 1
        fi
        ;;
esac
exit 0
EOF

cat >"${package_root}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    pkill -TERM -f '^/usr/libexec/kydisplay-daemon$' 2>/dev/null || true

    # What the distribution cannot supply. --system-site-packages so the
    # apt-provided PyQt6, gi and dbus stay visible: only yt-dlp and
    # curl_cffi belong in here. The extras are the combination yt-dlp's
    # own project recommends, and curl-cffi is what gives it the
    # impersonation that some sites need before they will answer.
    venv=/opt/kydisplay/venv
    if [ ! -x "${venv}/bin/python3" ]; then
        python3 -m venv --system-site-packages "${venv}"
    fi
    if ! "${venv}/bin/python3" -m pip install --quiet --upgrade \
            "yt-dlp[default,curl-cffi]"; then
        echo "kydisplay: could not fetch yt-dlp into ${venv}." >&2
        echo "  Casting will not resolve streams until this succeeds." >&2
        echo "  Retry with: sudo kydisplay --update" >&2
        exit 1
    fi
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF

cat >"${package_root}/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    pkill -TERM -f '^/usr/libexec/kydisplay-daemon$' 2>/dev/null || true
fi
# Only on purge. dpkg does not know about this directory — postinst
# built it — so nothing else will ever take it away, but a plain remove
# should leave it: reinstalling then needs no network, and an upgrade
# on a disconnected machine still works.
if [ "$1" = "purge" ]; then
    rm -rf /opt/kydisplay
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF

chmod 0755 "${package_root}/DEBIAN/preinst" \
    "${package_root}/DEBIAN/postinst" "${package_root}/DEBIAN/postrm"
(cd "${package_root}" && find usr -type f -print0 | sort -z | xargs -0 md5sum >DEBIAN/md5sums)
find "${package_root}" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
dpkg-deb --root-owner-group --build "${package_root}" \
    "${output_dir}/${package_name}_${version}_${architecture}.deb"
