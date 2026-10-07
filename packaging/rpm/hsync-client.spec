%global _hsync_pkgdir %{pkgdir}

Name:           hsync-client
Version:        __VERSION__
Release:        1%{?dist}
Summary:        HSYNC - file synchronization client

License:        BSD-3-Clause
URL:            https://github.com/albertsdk/hsync-client
Packager:       Alberto Basso <17047798+albertsdk@users.noreply.github.com>

# The binary is a PyInstaller onedir bundle that carries its own Python runtime
# and private shared libraries, so rpm's automatic dependency scanner would
# produce bogus requires/provides by walking those bundled .so files. Declare
# the real runtime requirements explicitly instead.
AutoReqProv:    no
Requires:       glibc
Requires:       systemd
Requires(pre):  shadow-utils

%description
HSYNC keeps directories in sync across machines and NAS devices.

Supports p2p peer-to-peer (no cloud) and remote server modes with real-time
file watching, compression, and bandwidth control.

%prep
# No source archive: the payload is a staged buildroot supplied by compile.sh
# and copied verbatim below.
exit 0

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}
cp -a %{_hsync_pkgdir}/. %{buildroot}/

%pre
getent group _hsync >/dev/null 2>&1 || groupadd -r _hsync
if ! id _hsync >/dev/null 2>&1; then
    useradd -r -g _hsync -s /sbin/nologin -d /nonexistent \
        -c "HSYNC file synchronization client" _hsync
fi
exit 0

%post
mkdir -p %{_sysconfdir}/hsync %{_localstatedir}/log/hsync \
    %{_localstatedir}/lib/hsync/device %{_localstatedir}/lib/hsync/state \
    %{_localstatedir}/lib/hsync/locks
chown root:root %{_sysconfdir}/hsync 2>/dev/null || true
chmod 0750 %{_sysconfdir}/hsync
chown _hsync:_hsync %{_localstatedir}/log/hsync 2>/dev/null || true
chmod 0750 %{_localstatedir}/log/hsync
chown _hsync:_hsync %{_localstatedir}/lib/hsync 2>/dev/null || true
chmod 0750 %{_localstatedir}/lib/hsync
chown _hsync:_hsync %{_localstatedir}/lib/hsync/device 2>/dev/null || true
chmod 0700 %{_localstatedir}/lib/hsync/device
chown _hsync:_hsync %{_localstatedir}/lib/hsync/state \
    %{_localstatedir}/lib/hsync/locks 2>/dev/null || true
chmod 0750 %{_localstatedir}/lib/hsync/state \
    %{_localstatedir}/lib/hsync/locks
chown root:root %{_sysconfdir}/hsync/hsync.conf 2>/dev/null || true
chmod 0600 %{_sysconfdir}/hsync/hsync.conf 2>/dev/null || true
command -v restorecon >/dev/null 2>&1 && \
    restorecon -R %{_sysconfdir}/hsync %{_localstatedir}/lib/hsync \
    %{_localstatedir}/log/hsync /usr/lib/systemd/system/hsync.service 2>/dev/null || true
# Register the unit but do NOT enable or start it: the administrator opts in
# with ``systemctl enable --now hsync.service``.
systemctl daemon-reload 2>/dev/null || true
exit 0

%preun
if [ "$1" -eq 0 ]; then
    systemctl stop hsync.service 2>/dev/null || true
    systemctl disable hsync.service 2>/dev/null || true
fi
exit 0

%postun
systemctl daemon-reload 2>/dev/null || true
exit 0

%files
%config(noreplace) %attr(0600,root,root) %{_sysconfdir}/hsync/hsync.conf
%dir %attr(0750,root,root) %{_sysconfdir}/hsync
%{_prefix}/lib/hsync
/usr/bin/hsync
%{_mandir}/man1/hsync.1.gz
/usr/lib/systemd/system/hsync.service

%changelog
