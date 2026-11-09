#!/usr/bin/env python3
"""TVPort backend: builds a Switch game you own into an Apple TV app, installs it, and reads back how it ran.

The Mac app drives this; it also runs on its own. Commands print JSON: `devices`, `inspect` and `diagnose` one object, `build`
a stream of one object per line, {"event": "stage" | "log" | "progress" | "error" | "done", ...}.

The game goes through nxnative as usual (scripts/build_ipa_from_nsp.py makes <title ID>_build: rewritten modules and shaders), then
build_ipa.sh with DM_TVOS=1 makes a separate tvOS project beside them. A build folder from an earlier run is reused, so only the first build of a game takes long.

In a checkout this file runs the engine around it. In TVPort.app (scripts/package_tvport.py) the engine comes as an archive beside this file, with engine.json, and `setup`
unpacks it into TVPort's own folder and makes the Python it needs there.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
BUNDLED = (HERE / 'engine.json').is_file()
REPO = HERE.parent   # the engine: this checkout, or for the app the copy it unpacked (main)
TITLE_RE = re.compile(r'^([0-9A-Fa-f]{16})_build$')
SOURCE_SUFFIXES = {'.nsp', '.xci', '.nsz', '.xcz'}
ENGINE_PACKAGES = ('fontTools', 'capstone', 'lz4')
SUPPORT = Path.home() / 'Library/Application Support'
DATA = Path(os.environ.get('TVPORT_DATA') or SUPPORT / 'TVPort')   # the app's library, the build lock, and the app's engine and Python; TVPORT_DATA moves it, for a test run
PYTHON_VERSION = '3.14'
REQUIREMENTS = HERE / 'requirements.txt'
TOOL_PATHS = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/local/share/dotnet', str(Path.home() / '.dotnet')]   # where the NSP script looks too
TEXTURE_CAPS = (0, 1024, 512, 256)


def render_scale_arg(text: str) -> float:
    """0.5 to 1.0, the range the loader accepts (DM_RENDER_SCALE)"""
    value = float(text)
    if not 0.5 <= value <= 1.0:
        raise argparse.ArgumentTypeError('the render scale is 0.5 to 1.0')
    return value
CACHES = 'Library/Caches'
SHADER_LIST = 'tvport-shaders.tsv'
SAVE_BACKUPS = 'save-backups'
KEEP_BACKUPS = 20   # the newest backups, whatever their age,
KEEP_DAYS = 30      # and the newest of each day for this many days: a backup every few minutes would otherwise reach back barely an hour
BACKUP_NAME = re.compile(r'^\d{4}-\d{2}-\d{2}')   # a backup folder: the local time it was made, so names sort by age
MANIFEST = 'tvport-manifest.json'   # the TV's listing a backup was copied from
ICLOUD = Path.home() / 'Library/Mobile Documents/com~apple~CloudDocs'
ICLOUD_SAVES = 'TVPort Saves'
TV_TIMEOUT_S = 60
LAUNCH_SETTLE_S = 3
SAVE_FILE = re.compile(r'^save(\.p-[a-z0-9-]+)?(_device)?/.')   # each player's committed save; save.work, save.new and the like are working copies


REASON = re.compile(r' error: |\[RED\]|FAIL: ')   # lines that say why a tool stopped; xcodebuild's own last lines only say that it did


class Failure(Exception):
    """A problem the person can act on; its text is shown as it is."""


def emit(event: str, **fields) -> None:
    print(json.dumps({'event': event, **fields}), flush=True)


def stage(name: str) -> None:
    emit('stage', name=name)


def run(argv: Iterable, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], text=True, capture_output=True, **kwargs)


def stream(argv: Iterable, *, env=None, cwd=None, on_line=None, log_filter=None) -> None:
    """Run a command, passing its output on as log events; stop with its last lines when it fails."""
    process = subprocess.Popen([str(a) for a in argv], env=env, cwd=cwd, text=True, errors='replace', stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail: list = []
    reasons: list = []
    for raw in process.stdout:
        line = raw.rstrip('\n')
        tail = (tail + [line])[-25:]
        if REASON.search(line):
            reasons = (reasons + [line.strip()])[-12:]
        if on_line:
            on_line(line)
        if log_filter is None or log_filter(line):
            emit('log', line=line[:400])
    if process.wait():
        raise Failure('\n'.join(reasons or tail[-6:])[-1500:])


def python_stamp() -> str:
    """What TVPort's own environment was made from: a new release that changes it makes it again."""
    return f'python {PYTHON_VERSION}\n' + REQUIREMENTS.read_text()


def venv_ready() -> bool:
    stamp = DATA / 'venv/tvport-requirements.txt'
    return (DATA / 'venv/bin/python3').exists() and stamp.is_file() and stamp.read_text() == python_stamp()


def borrowed_python() -> Optional[str]:
    """An interpreter with the engine's packages, 3.12 or newer (the NSP script's minimum): TVPort's own environment, then the one NX Play installed, then Homebrew's or the system's."""
    candidates = ([DATA / 'venv/bin/python3'] if venv_ready() else []) + [SUPPORT / 'NX Play/venv/bin/python3', Path('/opt/homebrew/bin/python3'), Path(shutil.which('python3') or 'python3')]
    probe = 'import sys; assert sys.version_info >= (3, 12); ' + '; '.join(f'import {p}' for p in ENGINE_PACKAGES)
    for python in candidates:
        if python.exists() and run([python, '-c', probe]).returncode == 0:
            return str(python)
    return None


def engine_python() -> str:
    python = borrowed_python()
    if python:
        return python
    if uv_tool():
        return set_up_python()
    raise Failure(f'No Python {PYTHON_VERSION} with the engine\'s packages, and no uv to make one. In Terminal: brew install uv, then press Set Up in TVPort.')


def uv_tool() -> Optional[str]:
    """uv makes TVPort's Python: the app carries one (Contents/Helpers), a checkout takes Homebrew's."""
    carried = HERE.parent / 'Helpers/uv'
    return str(carried) if carried.is_file() else shutil.which('uv', path=os.pathsep.join(TOOL_PATHS + [os.environ.get('PATH', '')]))


def set_up_python() -> str:
    """TVPort's own Python and the engine's packages at the versions requirements.txt names, all inside TVPort's folder: uv puts nothing in ~/.local or a cache outside it.
    The download needs the internet, once per release that changes them."""
    uv, venv = uv_tool(), DATA / 'venv'
    if not uv:
        raise Failure('uv is missing, so TVPort cannot make its Python. In Terminal: brew install uv')
    DATA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(DATA / 'python'), UV_NO_CONFIG='1', UV_NO_CACHE='1', UV_NO_PROJECT='1', UV_PYTHON_PREFERENCE='only-managed')
    env.pop('VIRTUAL_ENV', None)
    try:
        stage(f'Downloading Python {PYTHON_VERSION} for the engine')
        stream([uv, 'python', 'install', PYTHON_VERSION, '--no-bin'], env=env, cwd=DATA)
        stage('Installing the engine\'s Python packages')
        stream([uv, 'venv', '--clear', '--python', PYTHON_VERSION, venv], env=env, cwd=DATA)
        stream([uv, 'pip', 'install', '--python', venv / 'bin/python3', '-r', REQUIREMENTS], env=env, cwd=DATA)
    except Failure as failure:
        raise Failure(f'TVPort could not set up its Python. It needs the internet the first time.\n{failure}') from failure
    (venv / 'tvport-requirements.txt').write_text(python_stamp())
    return str(venv / 'bin/python3')


def unpack_engine(bundle: Path, data: Path) -> Path:
    """The engine the app carries, unpacked once per release into TVPort's folder: its tools write beside themselves (a profile's shader tables, the extractor's build files), which a
    signed app must not do to itself. Another command of the app may be unpacking it at the same moment, so the second waits for the first."""
    engine_id = json.loads((bundle / 'engine.json').read_text())['id']
    root = data / 'engine'
    target = root / engine_id
    if target.is_dir():
        return target
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_dir():
            return target
        partial = root / f'.{engine_id}.partial'
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        result = run(['/usr/bin/tar', '-xf', bundle / 'engine.tar.xz', '-C', partial])
        if result.returncode:
            shutil.rmtree(partial, ignore_errors=True)
            raise Failure(f'The engine TVPort carries could not be unpacked into {root}: {result.stderr.strip()[-500:]}')
        shutil.copy2(bundle / 'engine.json', partial / 'engine.json')
        partial.rename(target)
    return target


def prune_engines(keep: Path) -> None:
    """Earlier releases' engines. Called while holding the build lock, so no build is using one."""
    for old in keep.parent.iterdir():
        if old.is_dir() and old != keep:
            shutil.rmtree(old, ignore_errors=True)


def chain() -> str:
    match = re.search(r'#define DMSH_CHAIN "(.*)"', (REPO / 'dm/gfx/nvn/dmsh_chain.h').read_text())
    if not match:
        raise Failure('The engine has no shader chain version (dm/gfx/nvn/dmsh_chain.h)')
    return match.group(1)


def nacp_text(path: Path, flag: Optional[str] = None) -> str:
    result = run([sys.executable, '-I', REPO / 'dm/tools/nacp.py'] + ([flag] if flag else []) + [path])
    return result.stdout.strip() if result.returncode == 0 else ''


class Game:
    """One game folder and what earlier builds left in it."""

    def __init__(self, folder: Path):
        self.folder = folder.expanduser().resolve()
        if not self.folder.is_dir():
            raise Failure(f'No such folder: {self.folder}')
        self.title_id = ''
        self.build = None
        builds = sorted(p for p in self.folder.iterdir() if p.is_dir() and TITLE_RE.match(p.name))
        if builds:
            self.build = builds[0]
            self.title_id = TITLE_RE.match(self.build.name).group(1)

    @property
    def sources(self) -> list:
        return sorted(p.name for p in self.folder.iterdir() if p.suffix.lower() in SOURCE_SUFFIXES)

    @property
    def fingerprint(self) -> list:
        """The game files as of now (name and size): a DLC or update added after the first build changes it, and the engine then extracts the new file. Everything it already built is reused."""
        return sorted(f'{p.name}:{p.stat().st_size}' for p in self.folder.iterdir() if p.suffix.lower() in SOURCE_SUFFIXES)

    @property
    def package(self) -> Optional[str]:
        patched = sorted(self.build.glob('patched_*')) if self.build else []
        patched = [p for p in patched if p.is_dir()]
        return patched[-1].name[len('patched_'):] if patched else None

    def fixup_declared(self, key: str) -> bool:
        """Whether the rewritten main's fixups declare `# <key>`, as the iPhone launcher's fixups: conditions read them (dm/games/settings.tsv)"""
        fixups = self.build / f'patched_{self.package}/main.fixups.tsv' if self.build and self.package else None
        return bool(fixups and fixups.is_file() and any(line.startswith(f'# {key}\t') or line == f'# {key}' for line in fixups.read_text(errors='replace').splitlines()))

    def render_scale_supported(self) -> bool:
        """A render-scale stub in the rewritten main (Tears of the Kingdom 1.4.2 / 1.4.3, Breath of the Wild 1.9.0)"""
        return self.fixup_declared('RENDER_SCALE')

    def profile_value(self, key: str) -> str:
        """A key of the game's profile: the derived one the NSP script wrote for a version it was not brought up on, else the repository's (dm/games/<title ID>/game.tsv)"""
        for tsv in ((self.build / 'profile' / self.title_id / 'game.tsv') if self.build else None, REPO / 'dm/games' / self.title_id / 'game.tsv'):
            if tsv and tsv.is_file():
                for line in tsv.read_text(errors='replace').splitlines():
                    fields = line.split('\t')
                    if len(fields) >= 2 and fields[0] == key:
                        return fields[1].strip()
        return ''

    def frame_step_supported(self) -> bool:
        """A frame step in the rewritten main (its state page, FPS_STATE_OFF): the host decides each frame's step and holds 30 fps while the device is thermally serious (hle_nvn.c thermal_capped)"""
        return self.fixup_declared('FPS_STATE_OFF')

    @property
    def extract(self) -> Optional[Path]:
        return self.build / 'extract' / self.package if self.package else None

    @property
    def romfs(self) -> Optional[Path]:
        if not self.extract:
            return None
        image = self.extract / 'RomFS.img'
        return image if image.is_file() else None

    @property
    def shaders(self) -> Optional[Path]:
        if not self.build:
            return None
        folder = self.build / 'shader/ios'
        return folder if (folder / f'b-{chain()}/inputs.tsv').is_file() else None

    @property
    def prepared(self) -> bool:
        """Built, and for the game files in the folder now. A build from before TVPort noted its files has no list to compare, so it counts as current."""
        seen = self.state().get('sources')
        return bool(self.package and self.romfs and self.shaders) and (seen is None or seen == self.fingerprint)

    @property
    def state_file(self) -> Optional[Path]:
        return self.build / 'tvport.json' if self.build else None

    def state(self) -> dict:
        if self.state_file and self.state_file.is_file():
            return json.loads(self.state_file.read_text())
        return {}

    def save_state(self, **fields) -> None:
        self.state_file.write_text(json.dumps({**self.state(), **fields}, indent=1))

    @property
    def icon(self) -> Optional[Path]:
        icon = self.extract / 'icon.jpg' if self.extract else None
        return icon if icon and icon.is_file() else None

    @property
    def app(self) -> Optional[Path]:
        return self.build / 'tvport-derived/Build/Products/Release-appletvos/DmIOS.app' if self.build else None

    def earlier_app(self) -> Optional[Path]:
        """The app of a build made before this project: its bundle ID keeps the game data already on the TV."""
        for app in (self.app, self.build / 'xcode-derived-data/Build/Products/Release-appletvos/DmIOS.app' if self.build else None):
            if app and (app / 'Info.plist').is_file():
                return app
        return None

    def bundle_id(self, team: str) -> str:
        saved = self.state().get('bundle_id')
        if saved:
            return saved
        app = self.earlier_app()
        if app:
            return plistlib.loads((app / 'Info.plist').read_bytes())['CFBundleIdentifier']
        return f'dev.tvport.{team.lower()}.g{self.title_id.lower()}'

    def name(self) -> str:
        control = self.extract / 'control.nacp' if self.extract else None
        return (nacp_text(control) if control and control.is_file() else '') or self.folder.name

    def version(self) -> str:
        control = self.extract / 'control.nacp' if self.extract else None
        return nacp_text(control, '--version') if control and control.is_file() else ''


def provisioning(app: Optional[Path]) -> dict:
    """What a signed app's profile says: its team, and when tvOS stops opening it."""
    profile = app / 'embedded.mobileprovision' if app else None
    if not profile or not profile.is_file():
        return {}
    decoded = run(['security', 'cms', '-D', '-i', profile])
    if decoded.returncode:
        return {}
    data = plistlib.loads(decoded.stdout.encode())
    expires = data['ExpirationDate'].replace(tzinfo=timezone.utc)
    return {'team': data['TeamIdentifier'][0], 'expires': expires.isoformat(), 'days_left': (expires - datetime.now(timezone.utc)).days,
            'free_team': bool(data.get('LocalProvision')) or (expires - data['CreationDate'].replace(tzinfo=timezone.utc)).days <= 7}


def xcode_teams(prefs: dict) -> list:
    """The teams of the Apple IDs signed in to Xcode (Settings > Accounts), personal teams first: a free Apple ID that has never built anything has no profile yet, only this."""
    teams = [team for account in prefs.get('IDEProvisioningTeamByIdentifier', {}).values() for team in account if team.get('teamID')]
    return [team['teamID'] for team in sorted(teams, key=lambda team: not team.get('isFreeProvisioningTeam'))]


def known_team() -> str:
    folders = [Path.home() / 'Library/Developer/Xcode/UserData/Provisioning Profiles', Path.home() / 'Library/MobileDevice/Provisioning Profiles']
    for folder in folders:
        for profile in sorted(folder.glob('*.mobileprovision'), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []:
            decoded = run(['security', 'cms', '-D', '-i', profile])
            if decoded.returncode == 0:
                return plistlib.loads(decoded.stdout.encode())['TeamIdentifier'][0]
    exported = run(['defaults', 'export', 'com.apple.dt.Xcode', '-'])
    try:
        teams = xcode_teams(plistlib.loads(exported.stdout.encode())) if exported.returncode == 0 else []
    except plistlib.InvalidFileException:
        teams = []
    return teams[0] if teams else ''


def devicectl_json(*args, timeout: Optional[float] = None) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / 'out.json'
        try:
            result = run(['xcrun', 'devicectl', *args, '--quiet', '--json-output', out], timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Failure('The Apple TV did not answer.') from None
        if not out.is_file():
            raise Failure(result.stderr.strip() or 'devicectl gave no answer. Is Xcode installed?')
        return json.loads(out.read_text())


def apple_tvs(listing: dict) -> list:
    tvs = []
    for device in listing.get('result', {}).get('devices', []):
        hardware = device.get('hardwareProperties', {})
        if hardware.get('deviceType') != 'appleTV' or hardware.get('reality') != 'physical':
            continue
        properties, connection = device.get('deviceProperties', {}), device.get('connectionProperties', {})
        tvs.append({'id': hardware.get('udid'), 'name': properties.get('name'), 'model': hardware.get('marketingName'), 'os': properties.get('osVersionNumber'),
                    'connected': connection.get('tunnelState') == 'connected', 'developer_mode': properties.get('developerModeStatus') == 'enabled'})
    return tvs


def installed_files(device: str, bundle: str) -> dict:
    try:
        data = devicectl_json('device', 'info', 'files', '--device', device, '--domain-type', 'appDataContainer', '--domain-identifier', bundle, '--subdirectory', CACHES)
    except (Failure, json.JSONDecodeError):
        return {}
    return {f.get('relativePath'): f.get('metadata', {}).get('size') for f in data.get('result', {}).get('files', [])}


def cmd_devices(_) -> None:
    print(json.dumps({'devices': apple_tvs(devicectl_json('list', 'devices'))}))


def tool_path() -> str:
    return os.pathsep.join(TOOL_PATHS + [os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')])


def first_line(result: subprocess.CompletedProcess) -> str:
    return (result.stdout.strip() or result.stderr.strip()).split('\n')[0]


def checks() -> list:
    """What a build needs that the app cannot carry, each with what to do about it. The NSP script checks the same things (check_prereqs) once a build starts."""
    out = []

    def check(name: str, ok, detail: str, fix: str = '', command: str = '') -> bool:
        out.append({'name': name, 'ok': bool(ok), 'detail': detail, 'fix': '' if ok else fix, 'command': '' if ok else command})
        return bool(ok)

    developer = run(['xcode-select', '-p']).stdout.strip()
    xcode = run(['xcodebuild', '-version']) if developer and 'CommandLineTools' not in developer else None
    if check('Xcode', xcode and xcode.returncode == 0, ' '.join(xcode.stdout.split('\n')[0:1]) if xcode else 'Not installed, or not the selected developer folder',
             'Install Xcode from the App Store and open it once. If it is installed, select it:', 'sudo xcode-select -s /Applications/Xcode.app'):
        for sdk, platform in (('appletvos', 'tvOS'), ('iphoneos', 'iOS')):   # the shaders are iOS Metal libraries, so a tvOS build needs the iOS platform too
            version = run(['xcrun', '--sdk', sdk, '--show-sdk-version'])
            check(f'{platform} platform', version.returncode == 0, f'SDK {version.stdout.strip()}' if version.returncode == 0 else 'Not installed',
                  'Xcode > Settings > Components, or in Terminal:', f'xcodebuild -downloadPlatform {platform}')
        metal = run(['xcrun', '-sdk', 'iphoneos', 'metal', '--version'])
        check('Metal toolchain', metal.returncode == 0, first_line(metal) if metal.returncode == 0 else 'Not installed', 'In Terminal:', 'xcodebuild -downloadComponent MetalToolchain')
    team = known_team()
    check('Apple ID', team, f'Signing team {team}', 'Open Xcode, then Settings > Accounts, and add your Apple ID. A free one works: its installs open for 7 days.')
    path = tool_path()
    missing = [formula for tool, formula in (('xcodegen', 'xcodegen'), ('zstd', 'zstd'), ('glslangValidator', 'glslang')) if not shutil.which(tool, path=path)]
    brew = shutil.which('brew', path=path)
    check('Build tools', not missing, 'xcodegen, zstd and glslang' if not missing else 'Missing: ' + ', '.join(missing),
          'In Terminal:' if brew else 'Install Homebrew from brew.sh, then in Terminal:', 'brew install ' + ' '.join(missing))
    dotnet = shutil.which('dotnet', path=path)
    sdks = run([dotnet, '--list-sdks']).stdout.split('\n') if dotnet else []
    check('.NET SDK 10', any(line.startswith('10.') for line in sdks), next((line.split(' ')[0] for line in sdks if line.startswith('10.')), 'Not installed'),
          'It builds the game extractor and runs the shader translator. In Terminal:', 'brew install --cask dotnet-sdk')
    python = venv_ready() and str(DATA / 'venv/bin/python3') or borrowed_python()
    check('Python', python, f'Python {PYTHON_VERSION} in TVPort\'s folder' if venv_ready() else f'Borrowing {python}' if python else 'Not set up yet',
          'Press Set Up: TVPort downloads its own Python once.' if uv_tool() else 'TVPort makes its own Python with uv. In Terminal:', '' if uv_tool() else 'brew install uv')
    return out


def engine_info() -> dict:
    info = json.loads((REPO / 'engine.json').read_text()) if (REPO / 'engine.json').is_file() else {}
    return {'bundled': BUNDLED, 'folder': str(REPO), 'commit': info.get('commit', ''), 'chain': chain()}


def cmd_doctor(_) -> None:
    found = checks()
    own = not venv_ready() and bool(uv_tool()) and (BUNDLED or not borrowed_python())   # the app makes its own, rather than lean on another's that may change
    print(json.dumps({'checks': found, 'ready': all(c['ok'] for c in found), 'needs_setup': own,
                      'can_set_up': bool(uv_tool()), 'engine': engine_info()}))


def cmd_setup(_) -> None:
    """The app's first run, and the first run of each release: unpack its engine (main) and make its Python. Holds the build lock, as a build uses both."""
    lock = one_build_at_a_time()   # noqa: F841 held until this process exits
    if BUNDLED:
        prune_engines(REPO)
    if not venv_ready():
        set_up_python()
    emit('done', engine=engine_info())


def cmd_inspect(args) -> None:
    game = Game(args.folder)
    app = game.earlier_app() if game.build else None
    profile = provisioning(app)
    team = profile.get('team') or known_team()
    print(json.dumps({'folder': str(game.folder), 'title_id': game.title_id, 'name': game.name() if game.build else game.folder.name, 'version': game.version() if game.build else '',
                      'sources': game.sources, 'prepared': game.prepared, 'render_scale': game.render_scale_supported() if game.build else False, 'frame_step': game.frame_step_supported() if game.build else False, 'fps_target': int(game.profile_value('fps_target') or 0) if game.title_id else 0, 'icon': str(game.icon) if game.icon else None, 'team': team,
                      'bundle_id': game.bundle_id(team) if game.build and team else None, 'signing': profile, 'saved': game.state(),
                      'keys': str(find_keys(game) or '') or None, **backup_info(latest_backup(game))}))


def cmd_preview(args) -> None:
    result = run(['xcrun', 'swift', REPO / 'dm/ios/tools/tv_icon.swift', args.art, '--preview', args.out])
    if result.returncode:
        raise Failure(result.stderr.strip() or 'The artwork could not be read')
    print(json.dumps({'preview': str(args.out)}))


def find_keys(game: Game) -> Optional[Path]:
    """prod.keys in the usual places, so one copy serves every game."""
    for folder in (game.folder, game.folder.parent, Path.home() / 'Games', Path.home() / '.switch', Path.home() / 'Downloads', Path.home() / 'Desktop'):
        keys = folder / 'prod.keys'
        if keys.is_file():
            return keys
    return None


def save_listing(device: str, bundle: str) -> Optional[dict]:
    """The committed saves on the TV, as {path under Library/Caches: [size, last change]}, from one listing: empty for a game that is not installed, None for a TV that did not answer."""
    try:
        data = devicectl_json('device', 'info', 'files', '--device', device, '--domain-type', 'appDataContainer', '--domain-identifier', bundle, '--subdirectory', CACHES, timeout=TV_TIMEOUT_S)
    except (Failure, json.JSONDecodeError):
        return None
    if 'error' in data:
        return {} if data['error'].get('domain') == 'CoreDevice.ActionError' else None   # ActionError: the TV answered, and has no such app or folder
    return {f['relativePath']: [f.get('metadata', {}).get('size'), f.get('metadata', {}).get('lastModDate')] for f in data.get('result', {}).get('files', [])
            if SAVE_FILE.match(f.get('relativePath', '')) and not f.get('resources', {}).get('isDirectory')}


def save_files(device: str, bundle: str) -> list:
    """The committed saves on the TV, as paths under Library/Caches. An app that is not installed has none."""
    return sorted(save_listing(device, bundle) or {})


def backup_places(game: Game) -> list:
    """Where a game's backups are: beside the game, and in iCloud Drive when it is on (TVPort Saves/<title ID>)."""
    places = [game.build / SAVE_BACKUPS] if game.build else []
    if game.title_id and ICLOUD.is_dir():
        places.append(ICLOUD / ICLOUD_SAVES / game.title_id)
    return places


def newest_backup(places: Iterable[Path]) -> Optional[Path]:
    """The newest backup in any of these places: by name, which is the time it was made."""
    found = [p for place in places if place.is_dir() for p in place.iterdir() if p.is_dir() and BACKUP_NAME.match(p.name)]
    return max(found, key=lambda p: p.name, default=None)


def latest_backup(game: Game) -> Optional[Path]:
    return newest_backup(backup_places(game))


def backup_time(backup: Optional[Path]) -> Optional[float]:
    try:
        return datetime.strptime(backup.name[:17], '%Y-%m-%d-%H%M%S').timestamp() if backup else None
    except ValueError:
        return None


def read_manifest(backup: Path) -> Optional[dict]:
    try:
        return json.loads((backup / MANIFEST).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def same_files(one: Path, other: Path) -> bool:
    names = lambda root: sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and SAVE_FILE.match(str(p.relative_to(root))))
    return names(one) == names(other) and all((one / n).read_bytes() == (other / n).read_bytes() for n in names(one))


def kept_backups(names: Iterable[str], today) -> set:
    """Of a place's backup folders, those to keep: the newest KEEP_BACKUPS, and the newest of each of the last KEEP_DAYS days (today the newest)."""
    names = sorted(n for n in names if BACKUP_NAME.match(n))
    newest_of_day = {name[:10]: name for name in names}   # sorted, so each day ends on its newest
    first_day = (today - timedelta(days=KEEP_DAYS - 1)).isoformat()
    return set(names[-KEEP_BACKUPS:]) | {name for day, name in newest_of_day.items() if day >= first_day}


def prune_backups(place: Path) -> None:
    folders = [p for p in place.iterdir() if p.is_dir() and BACKUP_NAME.match(p.name)]
    keep = kept_backups([p.name for p in folders], datetime.now().date())
    for old in folders:
        if old.name not in keep:
            shutil.rmtree(old)


def backup_info(backup: Optional[Path]) -> dict:
    return {'last_backup': backup.name if backup else None, 'last_backup_time': backup_time(backup), 'last_backup_place': None if not backup else 'icloud' if ICLOUD in backup.parents else 'mac'}


def backup_saves(game: Game, device: str, bundle: str, icloud: bool = False, attempts: int = 3) -> dict:
    """Copies the TV's saves to the Mac, which survives what the TV's storage may not: an app deleted on the TV takes its preferences with it. With icloud, and iCloud Drive on,
    the copy goes to iCloud Drive instead. Only reads the TV: it lists files and copies them from it.
    The newest backup keeps the TV's listing it came from, and the same listing again copies nothing. A copy is kept only when a second listing after it matches the first: the
    game commits a save by swapping save.work into save, and a copy across that swap could mix old and new files. Identical saves make no new backup."""
    lock = (game.build / '.save-backup.lock').open('w')   # the app's rounds and a build's backup can meet
    fcntl.flock(lock, fcntl.LOCK_EX)
    newest, listing = latest_backup(game), None
    for _ in range(attempts):
        listing = save_listing(device, bundle)
        if not listing:
            return {'files': 0, 'folder': None, 'changed': False, 'state': 'unavailable' if listing is None else 'none'}
        if newest and read_manifest(newest) == listing:
            return {'files': len(listing), 'folder': str(newest), 'changed': False, 'state': 'unchanged'}
        incoming = Path(tempfile.mkdtemp(prefix='.save-backup-incoming-', dir=game.build))   # outside the backups, so a copy that fails partway is never taken for one
        try:
            for name in listing:
                (incoming / name).parent.mkdir(parents=True, exist_ok=True)
                run(['xcrun', 'devicectl', 'device', 'copy', 'from', '--device', device, '--domain-type', 'appDataContainer', '--domain-identifier', bundle, '--source', f'{CACHES}/{name}', '--destination', incoming / name])
                if not (incoming / name).is_file():
                    raise Failure(f'Could not copy {name} from the Apple TV. Nothing was changed.')
            if save_listing(device, bundle) != listing:
                continue   # the game saved during the copy
            if newest and same_files(incoming, newest):
                (newest / MANIFEST).write_text(json.dumps(listing))   # the same saves, written again: the next round needs no copy
                return {'files': len(listing), 'folder': str(newest), 'changed': False, 'state': 'unchanged'}
            (incoming / MANIFEST).write_text(json.dumps(listing))
            place = backup_places(game)[-1] if icloud else game.build / SAVE_BACKUPS
            stamp, n = datetime.now().strftime('%Y-%m-%d-%H%M%S'), 1
            target = place / stamp
            while target.exists() or target.parent.joinpath(f'.{target.name}.partial').exists():
                n += 1
                target = place / f'{stamp}-{n}'
            partial = place / f'.{target.name}.partial'   # in the same place, so the backup appears whole
            partial.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(incoming, partial)
            partial.rename(target)
            prune_backups(place)
            return {'files': len(listing), 'folder': str(target), 'changed': True, 'state': 'backed-up'}
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
    return {'files': len(listing or {}), 'folder': str(newest) if newest else None, 'changed': False, 'state': 'busy'}


def restore_saves(game: Game, device: str, bundle: str) -> int:
    """Puts the newest backup back on a TV that has lost its saves. It never replaces a save that is there."""
    backup = latest_backup(game)
    if not backup or save_files(device, bundle):
        return 0
    names = [p for p in backup.rglob('*') if p.is_file() and SAVE_FILE.match(str(p.relative_to(backup)))]
    for path in names:
        result = run(['xcrun', 'devicectl', 'device', 'copy', 'to', '--device', device, '--domain-type', 'appDataContainer', '--domain-identifier', bundle, '--source', path, '--destination', f'{CACHES}/{path.relative_to(backup)}'])
        if result.returncode:
            raise Failure(f'Could not put {path.name} back on the Apple TV: {result.stderr.strip()[-300:]}')
    return len(names)


def write_env(game: Game, mode: str, texture_cap: int, players: str = '', render_scale: float = 1.0, thermal_cap: bool = True, fps_target: int = 0) -> Path:
    """The switches the app reads at launch (main.m, dm/env.txt)."""
    path = game.build / 'tvport-env.txt'
    lines = ['# Written by TVPort', f'DM_OPMODE={0 if mode == "handheld" else 1}', f'DM_BC_MAX_DIM={texture_cap}']
    if render_scale != 1.0:   # the 3D views at that fraction of the game's size (loader.c dm_fps_state_apply); a game without the stub ignores it
        lines.append(f'DM_RENDER_SCALE={render_scale:g}')
    if fps_target:   # the profile's fps_target replaced (30 or 60): the frame step's cap, or the engine limit the host sets (hle_fpslock.c)
        lines.append(f'DM_FPS_TARGET={fps_target}')
    if not thermal_cap:   # the frame step keeps its cap when the TV is hot; tvOS lowers the clocks then all the same (docs/reference/frame-rate.md)
        lines.append('DM_THERMAL_CAP=0')
    if players.strip():
        lines.append('DM_TV_PLAYERS=' + ' '.join(players.split()))
    path.write_text('\n'.join(lines) + '\n')
    return path


def profile_env(game: Game) -> dict:
    env = dict(os.environ)
    derived = game.build / 'profile'
    if (derived / game.title_id / 'game.tsv').is_file():
        env.update(DM_GAMES_DIR=str(derived), DM_GAMES_FIRST='1')
    return env


def restore_tables(game: Game) -> None:
    """c1_full.tsv and cb_extent.tsv are outputs of the shader build, kept in the profile, and a new checkout has none: take them from the game's last project."""
    profile = game.build / 'profile' / game.title_id
    if not profile.is_dir():
        profile = REPO / 'dm/games' / game.title_id
    for name in ('c1_full.tsv', 'cb_extent.tsv'):
        for origin in (game.build / 'tvport-xcode/Gen/dm' / name, game.build / 'xcode/Gen/dm' / name):
            if not (profile / name).is_file() and origin.is_file():
                shutil.copy2(origin, profile / name)


def prepare_game(game: Game, keys: Optional[Path], team: str, python: str) -> Game:
    stage('Extracting the game, rewriting its code and building shaders')
    emit('log', line='This is the long step: 30 to 60 minutes the first time, and once per game.')
    keys = keys if keys and keys.is_file() else find_keys(game)
    if not keys:
        raise Failure('This game has not been built yet, so it needs your prod.keys file')
    command = [python, REPO / 'scripts/build_ipa_from_nsp.py', game.folder, '-k', keys, '--prepare-xcode', '--keep-extracted', '--gpu', 'none', '--romfs', 'separate', '--allow-other-metal', '--progress', 'lines']
    if team:
        command += ['--team-id', team]
    stream(command, cwd=REPO)
    game = Game(game.folder)
    if game.build:
        game.save_state(sources=game.fingerprint)
    if not game.prepared:
        raise Failure('The engine finished without shaders or game files for this game. See the log above.')
    return game


def generate_project(game: Game, args, team: str, bundle: str, python: str) -> Path:
    stage('Generating the Apple TV project')
    restore_tables(game)
    env = profile_env(game)
    env.update(GAME=game.title_id, W=str(game.build), PATCHED=str(game.build / f'patched_{game.package}'), ROMFS=str(game.romfs), MSL_IOS=str(game.shaders), AOC=str(game.build / 'aoc') if (game.build / 'aoc').is_dir() else '',
               DM_TVOS='1', DM_COPY_ROMFS='NO', DM_SHOW_FPS='1' if args.fps else '0', DM_ENV_FILE=str(write_env(game, args.mode, args.texture_cap, args.players, args.render_scale, not args.no_thermal_cap, args.fps_target)), DM_BUNDLE_ID=bundle, DM_TEAM_ID=team,
               DM_XCODE_PROJECT_DIR=str(game.build / 'tvport-xcode'), DM_XCODE_DERIVED_DATA=str(game.build / 'tvport-derived'), FONT_PY=python)
    art = args.art or game.icon
    if art:
        env['DM_ICON'] = str(art)
    stream(['bash', REPO / 'dm/ios/tools/build_ipa.sh', '--prepare-xcode'], env=env, cwd=REPO)
    return game.build / 'tvport-xcode'


def merged_rows(*texts: str) -> list:
    """Shader list rows from every source, each once, in the order first seen."""
    rows = dict.fromkeys(line for text in texts for line in text.splitlines() if line.strip() and not line.startswith('#'))
    return list(rows)


def remember_shaders(game: Game, device: str, bundle: str) -> None:
    """The game records each shader and pipeline it meets on the TV, in Library/Caches, and warms them at launch. That file goes when the app or the cache goes, and the game then stalls on
    each one again, so a copy stays beside the game. A game that has not run yet has nothing to copy."""
    saved = game.build / SHADER_LIST
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / 'pso_user.tsv'
        run(['xcrun', 'devicectl', 'device', 'copy', 'from', '--device', device, '--domain-type', 'appDataContainer', '--domain-identifier', bundle, '--source', f'{CACHES}/pso_user.tsv', '--destination', copy])
        if copy.is_file():
            rows = merged_rows(saved.read_text() if saved.is_file() else '', copy.read_text())
            saved.write_text('\n'.join(rows) + '\n')


def bundle_shaders(game: Game, project: Path) -> None:
    """Puts the saved list into the app's own prewarm list, which the app reads next to the one on the TV."""
    saved, manifest = game.build / SHADER_LIST, project / 'Gen/dm/pso_manifest.tsv'
    if saved.is_file() and manifest.is_file():
        known = manifest.read_text()
        rows = merged_rows(saved.read_text())
        manifest.write_text(known.rstrip('\n') + '\n' + '\n'.join(row for row in rows if row not in set(merged_rows(known))) + '\n')
        emit('log', line=f'{len(rows)} known shaders and pipelines go into the app.')


def compile_app(game: Game, project: Path, device: Optional[str]) -> None:
    stage('Compiling and signing')
    schemes = sorted(p.stem for p in (project / 'DmIOS.xcodeproj/xcshareddata/xcschemes').glob('*.xcscheme'))
    if not schemes:
        raise Failure('The generated project has no scheme')
    total = max(len(list((project / 'Gen/src').iterdir())), 1) + 12
    done = [0]

    def on_line(line: str) -> None:
        if line.startswith('CompileC ') or line.startswith('CompileAssetCatalog') or line.startswith('Ld '):
            done[0] += 1
            emit('progress', value=min(done[0] / total, 0.99))

    command = ['xcodebuild', '-project', project / 'DmIOS.xcodeproj', '-scheme', schemes[0], '-configuration', 'Release', '-destination', f'platform=tvOS,id={device}' if device else 'generic/platform=tvOS',
               '-derivedDataPath', game.build / 'tvport-derived', '-allowProvisioningUpdates', 'build']
    stream(command, on_line=on_line, log_filter=lambda x: bool(REASON.search(x)) or x.startswith(('CodeSign', '** ', 'Copy game resources')))
    emit('progress', value=1.0)


def install(game: Game, device: str, bundle: str) -> None:
    stage('Installing on the Apple TV')
    if not game.app or not (game.app / 'embedded.mobileprovision').is_file():
        raise Failure('Build the app first: there is no signed app to install')
    result = run(['xcrun', 'devicectl', 'device', 'install', 'app', '--device', device, game.app])
    if result.returncode:
        raise Failure(result.stderr.strip()[-900:] or 'The install failed')
    stage('Checking the game files on the Apple TV')
    size = game.romfs.stat().st_size
    if installed_files(device, bundle).get('romfs.img') == size:
        emit('log', line='The game files are already on the Apple TV.')
        return
    emit('log', line=f'Sending the game files ({size / 2**30:.1f} GB). This takes a few minutes over the network.')
    stream(['xcrun', 'devicectl', 'device', 'copy', 'to', '--device', device, '--domain-type', 'appDataContainer', '--domain-identifier', bundle, '--source', game.romfs, '--destination', f'{CACHES}/romfs.img'])


def one_build_at_a_time():
    """Two builds would write the same project and race each other's install on the TV. The system drops this lock when the process ends, however it ends."""
    path = DATA / 'build.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Failure('Another TVPort build is already running. Wait for it to finish.')
    return lock


def cmd_build(args) -> None:
    lock = one_build_at_a_time()   # held until this process exits
    game = Game(args.folder)
    team = args.team or known_team()
    if not team:
        raise Failure('No Apple signing team found. Open Xcode, sign in under Settings > Accounts, and build any app for the Apple TV once.')
    if BUNDLED:
        prune_engines(REPO)
    python = engine_python()
    os.environ['PATH'] = os.pathsep.join([str(Path(python).parent), os.environ.get('PATH', '')])   # the engine's own `python3` calls (build_ipa.sh) take this one, not the system's 3.9
    if not game.prepared:
        game = prepare_game(game, args.keys, team, python)
    elif 'sources' not in game.state():
        game.save_state(sources=game.fingerprint)
    bundle = game.bundle_id(team)
    if args.device:
        remember_shaders(game, args.device, bundle)
        stage('Backing up saves to this Mac')
        backup = backup_saves(game, args.device, bundle, icloud=args.icloud)
        emit('log', line={'backed-up': f'{backup["files"]} save files backed up.', 'unchanged': 'The saves on this Mac are already up to date.', 'none': 'No saves on the Apple TV yet.',
                          'unavailable': 'The saves on the Apple TV could not be read, so this build goes ahead without a new backup.',
                          'busy': 'The game kept saving during each copy, so this build goes ahead without a new backup.'}[backup['state']])
    project = generate_project(game, args, team, bundle, python)
    bundle_shaders(game, project)
    compile_app(game, project, args.device)
    game.save_state(bundle_id=bundle, art=str(args.art) if args.art else None, mode=args.mode, texture_cap=args.texture_cap, render_scale=args.render_scale)
    if args.device:
        install(game, args.device, bundle)
        restored = restore_saves(game, args.device, bundle)
        if restored:
            emit('log', line=f'The Apple TV had lost its saves: {restored} files put back from this Mac.')
    emit('done', bundle_id=bundle, app=str(game.app), signing=provisioning(game.app))


def cmd_backup(args) -> None:
    game = Game(args.folder)
    if not game.build:
        raise Failure('This game has not been built yet, so it has no saves.')
    print(json.dumps(backup_saves(game, args.device, args.bundle, icloud=args.icloud)))


def build_running() -> bool:
    try:
        lock = one_build_at_a_time()
    except Failure:
        return True
    lock.close()
    return False


def cmd_autobackup(args) -> None:
    """One round of the app's automatic backups, for each built game in turn. It only reads the TV, as backup_saves does, and never launches, ends or installs anything.
    A round is skipped while a build runs, as each build backs up first, and ends at the first game the TV does not answer for: it is asleep or off the network."""
    out = {'state': 'done', 'games': {}, 'icloud': ICLOUD.is_dir()}
    if build_running():
        out['state'] = 'building'
    for folder in args.folder if out['state'] == 'done' else []:
        game = Game(Path(folder)) if Path(folder).is_dir() else None
        app = game.earlier_app() if game and game.build else None
        bundle = game and game.build and (game.state().get('bundle_id') or app and plistlib.loads((app / 'Info.plist').read_bytes())['CFBundleIdentifier'])
        if not bundle:
            continue   # never built, so never installed
        try:
            result = backup_saves(game, args.device, bundle, icloud=args.icloud, attempts=1)
        except Failure as failure:
            result = {'state': 'failed', 'message': str(failure)}
        out['games'][folder] = {'state': result['state'], 'checked': time.time() if result['state'] in ('backed-up', 'unchanged') else None, **backup_info(latest_backup(game))}
        if result['state'] == 'unavailable':   # asleep or off the network: each further game would wait as long
            out['state'] = 'unreachable'
            break
    print(json.dumps(out))


def running_pids(listing: dict, executable: str = 'DmIOS') -> list:
    return [p.get('processIdentifier') for p in listing.get('result', {}).get('runningProcesses', []) if f'/{executable}.app/' in str(p.get('executable', ''))]


def cmd_launch(args) -> None:
    # devicectl's --terminate-existing launches at once, and the Home screen coming back can then land on top of the new launch: tvOS suspends it during startup, which looks like a hang.
    # So the old instance goes first, and the Home screen gets a moment to settle.
    pids = running_pids(devicectl_json('device', 'info', 'processes', '--device', args.device))
    for pid in pids:
        run(['xcrun', 'devicectl', 'device', 'process', 'terminate', '--device', args.device, '--pid', pid])
    if pids:
        time.sleep(LAUNCH_SETTLE_S)
    result = run(['xcrun', 'devicectl', 'device', 'process', 'launch', '--device', args.device, args.bundle])
    if result.returncode:
        message = result.stdout + result.stderr
        raise Failure('The Apple TV is asleep. Wake it with the remote, then try again.' if 'asleep' in message else message.strip()[-700:])
    print(json.dumps({'launched': args.bundle}))


NUMBER = r'(-?[\d.]+)'
MEMORY_LINE = re.compile(rf'memory up to frame (\d+): resident {NUMBER} MB, GPU allocated {NUMBER} MB')
HEADROOM = re.compile(rf'headroom to the system memory limit {NUMBER} MB')
LONG_FRAME = re.compile(rf'\[longframe\] frame (\d+) \({NUMBER} s\) whole frame {NUMBER} ms, submit\+present {NUMBER} ms, GPU {NUMBER} ms; new pipelines (\d+) \+ shader libraries (\d+), {NUMBER} ms in all; '
                        rf'texture uploads (\d+) first \+ (\d+) re-uploads, {NUMBER} MB / {NUMBER} ms')
ON_SCREEN = re.compile(rf'on-screen up to frame (\d+): (\d+) frames, dropped (\d+).*?footprint (\d+) MiB')
HITCH_MS = 33.4   # a frame that misses two refreshes at 60 Hz


def read_log(text: str) -> dict:
    """What one run's log.txt says about memory and long frames."""
    memory = [(int(m[1]), float(m[2]), float(m[3])) for m in MEMORY_LINE.finditer(text)]
    headroom = [float(x) for x in HEADROOM.findall(text)]
    footprints = [int(m[4]) for m in ON_SCREEN.finditer(text)]
    frames = [{'frame': int(m[1]), 'ms': float(m[3]), 'gpu': float(m[5]), 'pipelines': float(m[8]), 'textures': float(m[12]), 'uploads': int(m[9])} for m in LONG_FRAME.finditer(text)]
    hitches = [f for f in frames if f['ms'] >= HITCH_MS]
    last_frame = max([m[0] for m in memory] + [int(m[1]) for m in ON_SCREEN.finditer(text)], default=0)
    return {'last_frame': last_frame, 'peak_mb': max([m[1] for m in memory] + footprints, default=0), 'gpu_mb': max((m[2] for m in memory), default=0), 'min_headroom_mb': min(headroom, default=None),
            'hitches': len(hitches), 'worst_ms': max((f['ms'] for f in frames), default=0), 'texture_ms': round(sum(f['textures'] for f in hitches), 1), 'pipeline_ms': round(sum(f['pipelines'] for f in hitches), 1),
            'gpu_slow_frames': len([f for f in frames if f['gpu'] > 16.7]), 'ended_on_precompile': text.rstrip().split('\n')[-1:] and 'parallel precompile' in text.rstrip().split('\n')[-1]}


def read_jetsam(folder: Path, process: str = 'DmIOS') -> list:
    """Times tvOS ended this app for memory; an out-of-memory kill leaves only these reports, no crash report."""
    kills = []
    for path in sorted(folder.rglob('JetsamEvent-*.ips')):
        lines = path.read_text(errors='replace').split('\n', 1)
        if len(lines) < 2:
            continue
        try:
            report = json.loads(lines[1])
        except json.JSONDecodeError:
            continue
        page = report.get('memoryStatus', {}).get('pageSize', 16384)
        for entry in report.get('processes', []):
            if entry.get('name') == process and entry.get('reason'):
                kills.append({'time': report.get('date', path.stem), 'reason': entry['reason'], 'mb': entry.get('rpages', 0) * page >> 20})
    return sorted(kills, key=lambda kill: kill['time'])   # oldest first by the report's own date: the newest can sit beside older ones in Retired/, whose path sorts after it


def verdicts(run_log: dict, kills: list) -> list:
    """Plain sentences for the person, each from a number above."""
    out = []
    if kills:
        last = kills[-1]
        out.append(f'tvOS ended the game for using too much memory ({last["mb"]:,} MB, {last["reason"]}) at {last["time"][:19]}. Most of that is the game itself, so Texture Detail helps only a little.')
    headroom = run_log['min_headroom_mb']
    if headroom is not None and headroom < 200:
        out.append(f'Memory is tight: {headroom:.0f} MB from the limit at its lowest. A scene load can end the game.')
    if run_log['hitches']:
        if run_log['texture_ms'] > run_log['pipeline_ms'] * 2:
            out.append(f'{run_log["hitches"]} long frames, mostly texture loading ({run_log["texture_ms"]:.0f} ms against {run_log["pipeline_ms"]:.0f} ms of shaders). Lower Texture Detail.')
        elif run_log['pipeline_ms'] > run_log['texture_ms'] * 2:
            out.append(f'{run_log["hitches"]} long frames, mostly first-use shaders ({run_log["pipeline_ms"]:.0f} ms). They fade as the game sees each scene once.')
        else:
            out.append(f'{run_log["hitches"]} long frames: {run_log["texture_ms"]:.0f} ms of texture loading, {run_log["pipeline_ms"]:.0f} ms of shaders.')
    if run_log['gpu_slow_frames']:
        out.append(f'{run_log["gpu_slow_frames"]} slow frames took the GPU over 16.7 ms. Handheld mode lowers its load.')
    if not out and run_log['last_frame']:
        out.append(f'Nothing unusual up to frame {run_log["last_frame"]:,}, with memory at {run_log["peak_mb"]:,.0f} MB. If the game closed by itself, tvOS most likely ended it for memory: a sudden spike can miss these readings, and tvOS does not always file a report.')
    return out


def cmd_diagnose(args) -> None:
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        copied = run(['xcrun', 'devicectl', 'device', 'copy', 'from', '--device', args.device, '--domain-type', 'appDataContainer', '--domain-identifier', args.bundle, '--source', f'{CACHES}/log.txt', '--destination', folder / 'log.txt'])
        if copied.returncode or not (folder / 'log.txt').is_file():
            raise Failure('The game has not left a log on the Apple TV yet. Play it once, then look again.')
        run(['xcrun', 'devicectl', 'device', 'copy', 'from', '--device', args.device, '--domain-type', 'systemCrashLogs', '--source', '.', '--destination', folder / 'reports'])
        log = (folder / 'log.txt').read_text(errors='replace')
        run_log, kills = read_log(log), read_jetsam(folder / 'reports')
        print(json.dumps({'run': run_log, 'kills': kills[-5:], 'verdicts': verdicts(run_log, kills[-1:]), 'log': log[-20000:]}))


def main() -> int:
    global REPO
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('devices')
    inspect = commands.add_parser('inspect')
    inspect.add_argument('folder', type=Path)
    preview = commands.add_parser('preview')
    preview.add_argument('--art', type=Path, required=True)
    preview.add_argument('--out', type=Path, required=True)
    build = commands.add_parser('build')
    build.add_argument('folder', type=Path)
    build.add_argument('--keys', type=Path)
    build.add_argument('--team')
    build.add_argument('--device')
    build.add_argument('--art', type=Path)
    build.add_argument('--mode', choices=('handheld', 'docked'), default='handheld')
    build.add_argument('--texture-cap', type=int, choices=TEXTURE_CAPS, default=512)
    build.add_argument('--render-scale', type=render_scale_arg, default=1.0)
    build.add_argument('--no-thermal-cap', action='store_true')
    build.add_argument('--fps-target', type=int, choices=(0, 30, 60), default=0)   # 0: the profile's
    build.add_argument('--fps', action='store_true')
    build.add_argument('--players', default='')
    build.add_argument('--icloud', action='store_true')
    backup = commands.add_parser('backup')
    backup.add_argument('folder', type=Path)
    backup.add_argument('--device', required=True)
    backup.add_argument('--bundle', required=True)
    backup.add_argument('--icloud', action='store_true', help='new backups go to iCloud Drive (TVPort Saves) when it is on')
    autobackup = commands.add_parser('autobackup')
    autobackup.add_argument('--device', required=True)
    autobackup.add_argument('--folder', action='append', default=[])
    autobackup.add_argument('--icloud', action='store_true')
    launch = commands.add_parser('launch')
    launch.add_argument('--device', required=True)
    launch.add_argument('--bundle', required=True)
    diagnose = commands.add_parser('diagnose')
    diagnose.add_argument('--device', required=True)
    diagnose.add_argument('--bundle', required=True)
    commands.add_parser('doctor')
    commands.add_parser('setup')
    args = parser.parse_args()
    streamed = args.command in ('build', 'setup')
    try:
        if BUNDLED:
            if args.command == 'setup':
                stage('Unpacking the engine')
            REPO = unpack_engine(HERE, DATA)
        {'devices': cmd_devices, 'inspect': cmd_inspect, 'preview': cmd_preview, 'build': cmd_build, 'backup': cmd_backup, 'launch': cmd_launch, 'diagnose': cmd_diagnose,
         'doctor': cmd_doctor, 'setup': cmd_setup, 'autobackup': cmd_autobackup}[args.command](args)
    except Failure as failure:
        if streamed:
            emit('error', message=str(failure))
        else:
            print(json.dumps({'error': str(failure)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
