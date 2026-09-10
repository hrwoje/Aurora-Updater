"""Edit existing one-line Niri bindings without rewriting other configuration."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

CONFIG = Path.home() / '.config/niri/config.kdl'
LINE = re.compile(r'^([ \t]*)([A-Za-z0-9_+.-]+)([^\n{]*)\{(.*)\}[ \t]*(?://[^\n]*)?$', re.M)

def read(path=CONFIG):
    text = path.read_text()
    start = re.search(r'^binds\s*\{\s*$', text, re.M)
    if not start:
        raise ValueError('Geen ondersteund binds-blok gevonden.')
    end = re.search(r'^\}', text[start.end():], re.M)
    if not end:
        raise ValueError('Einde van binds-blok ontbreekt.')
    stop = start.end() + end.start()
    body = text[start.end():stop]
    rows = []
    for match in LINE.finditer(body):
        rows.append(dict(key=match[2], options=match[3], action=match[4].strip(),
                         start=start.end()+match.start(), end=start.end()+match.end()))
    # Refuse to edit unsupported multiline structures rather than lose them.
    remainder = LINE.sub('', body)
    if any(line.strip() and not line.strip().startswith('//') for line in remainder.splitlines()):
        raise ValueError('Meerregelige/complexe sneltoetsconfiguratie: handmatig bewerken vereist.')
    return text, rows, stop

def update(key, action, original=None, expected=None, path=CONFIG):
    text, rows, end = read(path)
    if expected is not None and text != expected:
        raise ValueError('Configuratie is elders gewijzigd. Vernieuw eerst de lijst.')
    if not re.fullmatch(r'[A-Za-z0-9_+.-]+', key):
        raise ValueError('Gebruik een toetsnaam zoals Print of Mod+Shift+S.')
    if any(r['key'].lower() == key.lower() and r['key'] != original for r in rows):
        raise ValueError('Deze combinatie bestaat al. Selecteer die regel om te wijzigen.')
    old = next((r for r in rows if r['key'] == original), None)
    if original and old is None:
        raise ValueError('Sneltoets niet meer gevonden; vernieuw de lijst.')
    if '\n' in action or '\r' in action or not action.strip():
        raise ValueError('Geef één geldige Niri-actie op.')
    line = '    ' + key + (old['options'] if old else ' ') + '{ ' + action.strip().rstrip(';') + '; }'
    candidate = text[:old['start']] + line + text[old['end']:] if old else text[:end] + line + '\n' + text[end:]
    fd, temp = tempfile.mkstemp(prefix='.shortcuts-', suffix='.kdl', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(candidate)
            f.flush()
            os.fsync(f.fileno())
        check = subprocess.run(['niri', 'validate', '-c', temp], capture_output=True, text=True, timeout=15)
        if check.returncode:
            raise ValueError(check.stderr or check.stdout)
        if path.read_text() != text:
            raise ValueError('Configuratie veranderde tijdens validatie. Vernieuw eerst.')
        backup = path.with_name('config.kdl.shortcuts-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
        shutil.copy2(path, backup)
        os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
        # Apply immediately in a running session; failure here does not undo
        # the validated persistent file and Niri will use it next login.
        subprocess.run(['niri', 'msg', 'action', 'load-config-file'],
                       capture_output=True, text=True, timeout=15)
        return backup
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def build_page(app):
    from gi.repository import Gtk
    page = Gtk.ScrolledWindow()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for side in ('top', 'bottom', 'start', 'end'):
        getattr(box, 'set_margin_' + side)(20)
    page.set_child(box)
    box.append(app.create_page_header('Sneltoetsen', 'Bestaande Niri-combinaties bekijken en blijvend aanpassen', 'input-keyboard-symbolic'))
    note = Gtk.Label(label='Mod = Super/Windows-toets. Print = Print Screen. Wijzigingen gelden direct en na opnieuw aanmelden.\nProgrammacommando’s worden pas uitgevoerd als je de toets indrukt.', xalign=0)
    note.set_wrap(True)
    box.append(note)
    search = Gtk.SearchEntry(placeholder_text='Zoek op toets of commando')
    box.append(search)
    listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    scroll = Gtk.ScrolledWindow(min_content_height=240, vexpand=True)
    scroll.set_child(listing)
    box.append(scroll)
    key = Gtk.Entry(placeholder_text='Bijvoorbeeld Print of Mod+Shift+S')
    kind = Gtk.DropDown.new_from_strings(['Screenshot-selectietool', 'Hele scherm vastleggen', 'Venster vastleggen', 'Programma / shellcommando', 'Niri-actie (geavanceerd)'])
    command = Gtk.Entry(placeholder_text='Bijvoorbeeld gnome-terminal; bij Niri-actie: close-window')
    status = Gtk.Label(xalign=0, wrap=True, selectable=True)
    state = {'original': None, 'text': None, 'rows': []}
    for widget in (key, kind, command): box.append(widget)
    buttons = Gtk.Box(spacing=8)
    box.append(buttons)
    def render(*_):
        while listing.get_first_child(): listing.remove(listing.get_first_child())
        query = search.get_text().lower()
        for record in state['rows']:
            if query not in (record['key']+' '+record['action']).lower(): continue
            button = Gtk.Button(label=record['key']+' — '+record['action'])
            button.set_tooltip_text(record['action'])
            def select(_, row=record):
                state['original'] = row['key']
                key.set_text(row['key']); kind.set_selected(4); command.set_text(row['action'])
                status.set_text('Bewerken: '+row['key'])
            button.connect('clicked', select)
            listing.append(button)
    def refresh(*_):
        try:
            state['text'], state['rows'], _ = read()
            render()
            status.set_text(str(len(state['rows']))+' bestaande combinaties geladen.')
        except Exception as e: status.set_text(str(e))
    def new(*_):
        state['original'] = None; key.set_text(''); command.set_text(''); kind.set_selected(0)
        status.set_text('Nieuwe combinatie; bestaande combinaties worden niet overschreven.')
    def save(*_):
        actions = ['screenshot', 'screenshot-screen', 'screenshot-window', 'spawn-sh '+json.dumps(command.get_text(), ensure_ascii=False), command.get_text()]
        try:
            backup = update(key.get_text().strip(), actions[kind.get_selected()], state['original'], state['text'])
            state['original'] = key.get_text().strip()
            refresh()
            status.set_text('Opgeslagen en gevalideerd. Niri herleest de configuratie automatisch. Herstelkopie: '+backup.name)
        except Exception as e: status.set_text('Niet opgeslagen: '+str(e))
    for title, callback in [('Nieuwe sneltoets', new), ('Vernieuwen', refresh), ('Opslaan', save)]:
        button = Gtk.Button(label=title); button.connect('clicked', callback); buttons.append(button)
    search.connect('search-changed', render)
    box.append(status)
    refresh()
    return page
