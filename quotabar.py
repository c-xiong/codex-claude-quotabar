#!/usr/bin/env python3
"""A small, local-only terminal quota dashboard. Python standard library only."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty

ROOT = Path(__file__).resolve().parent
# QUOTABAR_BACKEND lets an existing CodexBar CLI install be reused instead of the bundled one.
BACKEND = Path(os.environ.get('QUOTABAR_BACKEND') or ROOT / 'backend' / 'CodexBarCLI')
# Gruvbox foregrounds only: the background is left to the terminal so the pane blends in.
DARK = {'text':'ebdbb2', 'muted':'a89984', 'line':'665c54', 'orange':'fe8019',
        'yellow':'fabd2f', 'green':'b8bb26', 'aqua':'8ec07c', 'blue':'83a598',
        'red':'fb4934', 'purple':'d3869b'}
LIGHT = {'text':'3c3836', 'muted':'7c6f64', 'line':'bdae93', 'orange':'af3a03',
         'yellow':'b57614', 'green':'79740e', 'aqua':'427b58', 'blue':'076678',
         'red':'9d0006', 'purple':'8f3f71'}
COLORS = DARK
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
SOURCES = {'codex': 'oauth', 'claude': 'cli'}
PROCESSES = set()
LOCK = threading.Lock()
STOP = threading.Event()


def stamp(value):
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def normalize(payload, provider):
    rows = payload if isinstance(payload, list) else [payload]
    row = next((r for r in rows if isinstance(r, dict) and r.get('provider') == provider), None)
    if not row:
        raise ValueError('No provider response')
    if row.get('error'):
        message = str(row['error']).lower()
        if any(s in message for s in ('credentials', 'authenticate', 'login', 'sign in', 'unauthorized', '401')):
            raise ValueError('Sign-in required: run ' + ('claude' if provider == 'claude' else 'codex login'))
        if '429' in message or 'rate limit' in message:
            raise ValueError('Temporarily rate limited; retrying automatically')
        raise ValueError('Usage unavailable; retrying automatically')
    usage = row.get('usage') or {}
    windows = []
    labels = row.get('rateWindowLabels') or {}
    for key in ('primary', 'secondary', 'tertiary'):
        w = usage.get(key)
        if not isinstance(w, dict):
            continue
        percent = w.get('usedPercent')
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
            percent = None
        minutes = w.get('windowMinutes')
        label = labels.get(key)
        if minutes == 300:
            label = '5-hour'
        elif minutes == 10080 and key != 'tertiary':
            label = 'Weekly'
        elif not label:
            label = 'Window ' + str(len(windows) + 1)
        label = ''.join(c for c in str(label) if c.isascii() and c.isprintable())[:22]
        windows.append({'label': label, 'used': percent, 'resets': stamp(w.get('resetsAt'))})
    if not windows:
        raise ValueError('No quota windows reported by provider')
    return {'windows': windows, 'updated': stamp(usage.get('updatedAt')) or time.time(),
            'source': row.get('source', SOURCES[provider])}


def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def fetch(provider):
    env = os.environ.copy()
    # The CLI probe only runs /usage in an isolated working directory, not in a project.
    env.pop('CLAUDECODE', None)
    process = subprocess.Popen([str(BACKEND), 'usage', '--provider', provider,
        '--source', SOURCES[provider], '--format', 'json', '--json-only'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(ROOT), env=env, start_new_session=True)
    with LOCK:
        PROCESSES.add(process)
        if STOP.is_set():
            terminate(process)
    try:
        try:
            output, _ = process.communicate(timeout=75)
        except subprocess.TimeoutExpired:
            terminate(process)
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise ValueError('Request timed out; retrying automatically')
        try:
            payload = json.loads(output)
        except (ValueError, TypeError):
            raise ValueError('Could not read usage; retrying automatically')
        return normalize(payload, provider)
    finally:
        with LOCK:
            PROCESSES.discard(process)


def countdown(target, now):
    if target is None:
        return ''
    if target <= now:
        return 'resetting'
    minutes = max(0, int(target - now)) // 60
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f'{days}d {hours}h'
    return f'{hours}h {minutes:02}m' if hours else f'{minutes}m'


def dark_mode():
    # macOS only sets AppleInterfaceStyle while Dark appearance is active.
    try:
        out = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return 'dark' in out.lower()


def set_theme(theme):
    global COLORS
    if theme == 'auto':
        theme = 'dark' if dark_mode() else 'light'
    COLORS = DARK if theme == 'dark' else LIGHT


def color(text, name='text', bold=False, enabled=True):
    if not enabled:
        return text
    rgb = tuple(int(COLORS[name][i:i+2], 16) for i in (0, 2, 4))
    return '\033[' + ('1;' if bold else '22;') + '38;2;%d;%d;%d;49m' % rgb + text


def visible(text):
    return len(ANSI.sub('', text))


def age_text(updated, now):
    age = max(0, int(now - updated))
    return f'{age}s ago' if age < 60 else f'{age // 60}m ago'


def render(states, width=40, interval=60, enabled=True, demo=False):
    now = time.time()
    width = max(35, min(width, 48))
    inner = width - 2
    lines = []
    c = lambda t, n='text', b=False: color(t, n, b, enabled)
    def add(text=''):
        lines.append(c(' ') + text + c(' ' * max(0, width - 1 - visible(text))))
    def row(left, right):
        add(left + c(' ' * max(1, inner - visible(left) - visible(right))) + right)
    for index, (provider, title, accent) in enumerate([
            ('codex', 'Codex', 'blue'), ('claude', 'Claude', 'purple')]):
        if index:
            add()
        state = states[provider]
        data = state.get('data')
        old = bool(data and now - data['updated'] > max(interval * 2, 120))
        # Only abnormal states get a colored word; a healthy feed just shows its age.
        if demo:
            status = c('demo', 'yellow')
        elif data and (state.get('error') or old):
            status = c('stale', 'yellow')
        elif state.get('busy'):
            status = c('refreshing', 'muted')
        elif state.get('error'):
            status = c('offline', 'red')
        else:
            status = ''
        if data and not state.get('busy'):
            status += c((' · ' if status else '') + age_text(data['updated'], now), 'muted')
        row(c(title, accent, True), status)
        if data:
            for window in data['windows']:
                pct = window['used']
                shade = 'muted' if pct is None else ('red' if pct >= 90 else 'yellow' if pct >= 70 else 'aqua')
                value = '–' if pct is None else f'{pct:.0f}%'
                reset = countdown(window['resets'], now)
                row(c(window['label'], 'text'),
                    c(f'{value:>4}', shade, True) + c(f'  ↻ {reset:>7}' if reset else '', 'muted'))
                filled = 0 if pct is None else round(max(0, min(100, pct)) / 100 * inner)
                # Half-block cells give a thick bar; plain mode keeps distinct glyphs since it has no color.
                full, empty = ('▄', '▄') if enabled else ('━', '─')
                add(c(full * filled, shade) + c(empty * (inner - filled), 'line'))
        elif not state.get('error'):
            add(c('fetching…', 'muted'))
        if state.get('error') and not data:
            import textwrap
            for line in textwrap.wrap(state['error'], inner):
                add(c(line, 'yellow'))
    return lines


def sample():
    now = time.time()
    return {p:{'data':{'updated':now-4,'source':'demo','windows':[
        {'label':'5-hour','used':a,'resets':now+5040},
        {'label':'Weekly','used':b,'resets':now+286200}]},'busy':False,'error':None}
        for p,a,b in [('codex',62,38),('claude',25,76)]}


def main():
    parser = argparse.ArgumentParser(description='Live Codex + Claude Code quota dashboard (Gruvbox).')
    parser.add_argument('--interval', type=int, default=60, help='refresh interval in seconds (minimum 30; default 60)')
    parser.add_argument('--once', action='store_true', help='print one snapshot and exit')
    parser.add_argument('--json', action='store_true', help='print one sanitized JSON snapshot and exit')
    parser.add_argument('--demo', action='store_true', help='preview the theme with clearly labelled sample data')
    parser.add_argument('--no-color', action='store_true', help='disable colors')
    theme = os.environ.get('QUOTABAR_THEME', 'auto').lower()
    parser.add_argument('--theme', choices=['auto', 'light', 'dark'],
                        default=theme if theme in ('auto', 'light', 'dark') else 'auto',
                        help='palette; auto follows the macOS appearance (default: $QUOTABAR_THEME or auto)')
    args = parser.parse_args()
    if args.interval < 30:
        parser.error('--interval must be at least 30 seconds')
    once = args.once or args.json or not sys.stdin.isatty() or not sys.stdout.isatty()
    if not args.demo and not BACKEND.exists():
        parser.error('CodexBar backend is missing; reinstall quotabar')
    results = queue.Queue()
    states = sample() if args.demo else {p:{'data':None,'error':None,'busy':False,'next':0} for p in SOURCES}
    def worker(provider):
        try:
            results.put((provider, fetch(provider), None))
        except (OSError, ValueError, subprocess.SubprocessError):
            # Never forward raw backend stderr, account identity or credentials.
            exc = sys.exc_info()[1]
            message = str(exc) if isinstance(exc,ValueError) else 'Could not start usage reader'
            results.put((provider, None, message))
    def launch(provider):
        states[provider]['busy'] = True
        threading.Thread(target=worker, args=(provider,), daemon=True).start()
    def drain():
        while True:
            try:
                provider, data, error = results.get_nowait()
            except queue.Empty:
                break
            state = states[provider]
            if data:
                state['data'] = data
            state.update(error=error, busy=False, next=time.monotonic()+args.interval)
    def stop_signal(signum, frame):
        STOP.set()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop_signal)
    settings = None
    interactive = False
    try:
        if not args.demo:
            for provider in SOURCES:
                launch(provider)
        if once:
            while any(s['busy'] for s in states.values()) and not STOP.is_set():
                drain()
                time.sleep(.1)
            if args.json:
                print(json.dumps({p:{'data':s['data'],'error':s['error']} for p,s in states.items()}, indent=2))
            else:
                print('\n'.join(render(states, interval=args.interval, enabled=False, demo=args.demo)))
            return 1 if STOP.is_set() or any(s.get('error') for s in states.values()) else 0
        settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        interactive = True
        sys.stdout.write('\033[?1049h\033[?25l\033[2J')
        previous = None
        last_manual = -100
        last_theme = -100
        while not STOP.is_set():
            drain()
            if time.monotonic() - last_theme >= 5:
                set_theme(args.theme)
                last_theme = time.monotonic()
            if not args.demo:
                for provider,state in states.items():
                    if not state['busy'] and time.monotonic() >= state['next']:
                        launch(provider)
            size = shutil.get_terminal_size((80,28))
            lines = render(states, size.columns-1, args.interval, not args.no_color, args.demo)
            if size.columns < 36 or size.lines < len(lines):
                lines = [f'Need 36 cols x {len(lines)} rows']
                lines = [line[:max(1,size.columns-1)] for line in lines[:max(1,size.lines-1)]]
            frame = '\033[H' + '\r\n'.join(line+'\033[K' for line in lines) + '\033[J'
            if frame != previous:
                sys.stdout.write(frame)
                sys.stdout.flush()
                previous = frame
            ready,_,_ = select.select([sys.stdin], [], [], 0.25)
            if ready:
                key = os.read(sys.stdin.fileno(), 32).lower()
                if not key or b'q' in key or b'\x03' in key or b'\x04' in key:
                    break
                if b'r' in key and not args.demo and time.monotonic()-last_manual >= 10:
                    last_manual=time.monotonic()
                    for provider,state in states.items():
                        if not state['busy']:
                            launch(provider)
        return 0
    finally:
        STOP.set()
        with LOCK:
            active = list(PROCESSES)
        for process in active:
            terminate(process)
        for process in active:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, settings)
        if interactive:
            sys.stdout.write('\033[0m\033[?25h\033[?1049l')
            sys.stdout.flush()


if __name__ == '__main__':
    sys.exit(main())
