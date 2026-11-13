#!/usr/bin/env python3
"""Non-invasive Wayland/X11 desktop portal diagnostics for local PCs.

Does not grant capture/control permission, change security settings, start
remote desktop, or attempt to inject desktop input. xdg-desktop-portal and the
GNOME Remote Desktop portal still require interactive desktop authorization.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def _run(argv: list[str], timeout: float = 4.0) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return completed.returncode, (completed.stdout or completed.stderr).strip()[:1000]
    except (OSError, subprocess.TimeoutExpired):
        return 127, 'Command unavailable or timed out'


def status(env=None, which=None, runner=None) -> dict:
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    runner = _run if runner is None else runner
    session_type = env.get('XDG_SESSION_TYPE', '').lower()
    display = bool(env.get('DISPLAY'))
    wayland = bool(env.get('WAYLAND_DISPLAY'))
    desktop = env.get('XDG_CURRENT_DESKTOP', '')
    session = env.get('XDG_SESSION_ID', '')
    if not session_type and session and which('loginctl'):
        rc, info = runner(['loginctl', 'show-session', session, '-p', 'Type', '--value'])
        if rc == 0:
            session_type = info.strip().lower()
    commands = {name: bool(which(name)) for name in
                ('systemctl', 'busctl', 'xdg-desktop-portal', 'pipewire', 'wireplumber',
                 'gnome-remote-desktop', 'dbus-update-activation-environment')}
    services = {}
    user_bus = bool(env.get('DBUS_SESSION_BUS_ADDRESS') or env.get('XDG_RUNTIME_DIR'))
    if user_bus and commands['systemctl'] and os.geteuid() != 0:
        for service in ('xdg-desktop-portal.service', 'pipewire.service', 'wireplumber.service'):
            rc, output = runner(['systemctl', '--user', 'is-active', service])
            services[service] = output if rc == 0 else 'unavailable/inactive'
    problems = []
    if not session_type:
        problems.append('No graphical login session detected; run from the desktop, not sudo/SSH')
    elif session_type == 'wayland':
        if not wayland:
            problems.append('WAYLAND_DISPLAY is unset in this process; check the desktop session environment')
        if not user_bus:
            problems.append('Desktop session bus unavailable; user portals cannot accept permission prompts')
        if not commands['xdg-desktop-portal']:
            problems.append('xdg-desktop-portal not found; install a matching desktop portal backend')
        if services.get('xdg-desktop-portal.service') == 'unavailable/inactive':
            problems.append('User xdg-desktop-portal service is not currently active')
    elif session_type == 'x11' and not display:
        problems.append('DISPLAY is unset in an X11 session')
    return {'session_type': session_type or 'unknown', 'desktop': desktop or 'unknown',
            'wayland_display': wayland, 'x11_display': display, 'user_bus_available': user_bus,
            'commands': commands, 'user_services': services, 'problems': problems,
            'permission': 'Wayland screen/input access requires explicit portal/GNOME user approval',
            'suggested_packages': ['xdg-desktop-portal', 'xdg-desktop-portal-gnome', 'pipewire', 'wireplumber']}


def repair_user_portal(info: dict, runner=None) -> dict:
    """Only refresh an already active portal after explicit user request."""
    runner = _run if runner is None else runner
    if os.geteuid() == 0:
        raise RuntimeError('Run without sudo, from your own desktop login session')
    if info['session_type'] != 'wayland' or not info['user_bus_available']:
        raise RuntimeError('Run inside an active Wayland session with a user bus')
    if not info['commands'].get('systemctl'):
        raise RuntimeError('systemctl is not installed')
    rc, msg = runner(['systemctl', '--user', 'try-restart', 'xdg-desktop-portal.service'])
    if rc != 0:
        raise RuntimeError('Could not refresh desktop portal: ' + msg[:300])
    return {'portal_refresh': 'requested',
            'note': 'No screen-share or input approval was granted. Approve access in your GNOME desktop.'}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--json', action='store_true', help='Machine-readable diagnostics')
    p.add_argument('--repair-user-portal', action='store_true',
                   help='Explicitly request user portal restart, never grants screen permissions')
    args = p.parse_args()
    info = status()
    if args.repair_user_portal:
        info['requested_repair'] = repair_user_portal(info)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print('Doomsday Vault desktop check — ' + info['session_type'] + ' / ' + info['desktop'])
        print('Display: Wayland=' + str(info['wayland_display']) + ', X11=' + str(info['x11_display']))
        for k, v in info['user_services'].items():
            print('User service ' + k + ': ' + v)
        for error in info['problems']:
            print('CHECK: ' + error)
        print('Wayland control is opt-in and requires desktop user authorization.')
        if info['problems']:
            print('Optional Ubuntu GNOME packages: ' + ' '.join(info['suggested_packages']))

if __name__ == '__main__':
    main()