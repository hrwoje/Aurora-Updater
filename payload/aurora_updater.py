#!/usr/bin/python3
"""Aurora component updater with hash checks and a small, fixed system layer."""
from __future__ import annotations
import argparse, fcntl, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
REPO="https://raw.githubusercontent.com/hrwoje/Aurora-Updater/main"; MANIFEST_URL=os.environ.get("AURORA_UPDATER_MANIFEST_URL",f"{REPO}/manifest.json")
STATE=Path(os.environ.get("XDG_STATE_HOME",Path.home()/".local/state"))/"aurora-updater"; INSTALLED=STATE/"installed.json"; HISTORY=STATE/"history.json"; BACKUPS=STATE/"backups"
ALLOWED_PREFIXES=tuple(Path.home()/x for x in (".local/bin",".config/aurora",".config/autostart",".local/share/applications",".local/share/icons",".local/share/fonts",".local/share/doc/aurora",".config/gtk-3.0",".config/gtk-4.0")); SYSTEM_PREFIXES=(Path("/etc/xbps.d"),Path("/etc/aurora")); PACKAGE_RE=re.compile(r"^[A-Za-z0-9+_.-]+$")
def emit(event,**data): print(json.dumps({"event":event,**data},ensure_ascii=False),flush=True)
def notify(title,message):
    if shutil.which("notify-send"):
        try: subprocess.run(["notify-send","--app-name=Aurora Updater",title,message],timeout=5,check=False)
        except (OSError,subprocess.SubprocessError): pass
def fetch(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Aurora-Updater/1"})
    with urllib.request.urlopen(req,timeout=12) as response: return response.read()
def resolve_manifest_url():
    """Lokale-bronterugval voor de ontwikkelmachine.

    Normaal wordt de manifest van de gepubliceerde GitHub-bron gehaald. Is die
    bron niet bereikbaar en staat er een lokale Aurora-werkrepo op deze machine,
    dan wordt die gebruikt (zelfde geverifieerde flow als de expliciete
    file://-modus). Op andere installaties verandert dit niets.
    """
    url=os.environ.get("AURORA_UPDATER_MANIFEST_URL",f"{REPO}/manifest.json")
    if url.startswith(REPO):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"Aurora-Updater/1"})
            with urllib.request.urlopen(req,timeout=12) as response: response.read(1)
        except Exception:
            local=Path.home()/"Aurora-Updater-work"/"manifest.json"
            if local.is_file(): url=local.as_uri()
    return url
LOCKFILE=STATE/"run.lock"

def acquire_lock():
    """Eén installatie tegelijk: een tweede gelijktijdige run wordt geweigerd."""
    STATE.mkdir(parents=True,exist_ok=True)
    handle=open(LOCKFILE,"a+",encoding="ascii")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle

def release_lock(handle):
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()

def _cleanup_stale_stage():
    """Ruim achtergebleven staging-mappen ouder dan een uur op (bijv. na een kill)."""
    cutoff=time.time()-3600
    for path in STATE.parent.glob("aurora-update-*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path,ignore_errors=True)
        except OSError:
            continue

def _prune_backups(keep=5):
    """Houd alleen de nieuwste reservekopieën en ruim oudere op."""
    if not BACKUPS.is_dir():
        return 0
    try:
        backups=sorted(BACKUPS.iterdir(),key=lambda p: p.stat().st_mtime)
    except OSError:
        return 0
    removed=0
    for old in backups[:-keep]:
        try:
            shutil.rmtree(old,ignore_errors=True)
            removed+=1
        except OSError:
            pass
    return removed

def manifest():
    data=json.loads(fetch(resolve_manifest_url()).decode())
    if data.get("schema")!=1 or not isinstance(data.get("version"),str) or not data.get("version"): raise ValueError("ongeldig Aurora-manifestschema")
    if "release_name" in data and (not isinstance(data["release_name"], str) or not data["release_name"].strip()): raise ValueError("ongeldige release-naam")
    for item in data.get("files",[])+data.get("system_files",[]):
        if not isinstance(item,dict) or not item.get("source") or not item.get("target") or not re.fullmatch(r"[0-9a-f]{64}",item.get("sha256","")): raise ValueError("ongeldige file-entry")
        source=Path(item["source"])
        if source.is_absolute() or ".." in source.parts: raise ValueError("ongeldig payloadpad")
    for key in ("required_packages","system_packages"):
        for package in data.get(key,[]):
            if not isinstance(package,str) or not PACKAGE_RE.fullmatch(package): raise ValueError(f"ongeldige pakketnaam: {package!r}")
    policy=data.get("system_policy",{})
    if not isinstance(policy,dict) or any(k not in ("refresh_xbps_metadata","refresh_certificates") for k in policy): raise ValueError("ongeldig systeembeleid")
    return data
def installed():
    try:return json.loads(INSTALLED.read_text(encoding="utf-8"))
    except (OSError,ValueError):return {}
def target_path(value):
    path=Path(os.path.expanduser(value)).resolve()
    if not any(path==root or root in path.parents for root in ALLOWED_PREFIXES):raise ValueError(f"doelpad valt buiten Aurora-allowlist: {path}")
    return path
def system_target_path(value):
    path=Path(value).resolve()
    if not any(path==root or root in path.parents for root in SYSTEM_PREFIXES):raise ValueError(f"systeemdoelpad valt buiten allowlist: {path}")
    return path
def version_key(value):
    """Versiestring naar sorteerbare sleutel (2026.09.10.15 -> [2026,9,10,15])."""
    key=[]
    for part in str(value).split("."):
        key.append(int(part) if part.isdigit() else part)
    return key

def missing_packages(data,key):
    result=[]
    for package in data.get(key,[]):
        if subprocess.run(["xbps-query","-p","pkgname",package],capture_output=True).returncode!=0:result.append(package)
    return result
def item_changed(item,system=False):
    target=system_target_path(item["target"]) if system else target_path(item["target"])
    return not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=item["sha256"]
def check():
    data,old=manifest(),installed(); files=[i["target"] for i in data.get("files",[]) if item_changed(i)]; system_files=[i["target"] for i in data.get("system_files",[]) if item_changed(i,True)]
    packages,system_packages=missing_packages(data,"required_packages"),missing_packages(data,"system_packages"); policy=data.get("system_policy",{}); actions=bool(system_files or system_packages) and bool(policy.get("refresh_xbps_metadata") or policy.get("refresh_certificates")); same_version=old.get("version")==data["version"]; downgrade=bool(old.get("version") and version_key(data["version"])<version_key(old["version"])); available=bool(files or system_files or packages or system_packages or actions or (old.get("version") and not same_version)) and not downgrade
    if downgrade:
        emit("status",message=f"De bron versie {data['version']} is ouder dan de geïnstalleerde versie {old['version']}; er wordt geen downgrade aangeboden.")
    emit("result",ok=True,version=data["version"],release_name=data.get("release_name",data["version"]),previous=old.get("version"),installed_at=old.get("installed_at"),history=old.get("history",[]),release_notes=data.get("release_notes",""),files=files,system_files=system_files,packages=packages,system_packages=system_packages,update_available=available,downgrade=downgrade); return 0
def run_root(args):
    result=subprocess.run(["pkexec",*args],text=True,capture_output=True)
    if result.returncode:raise RuntimeError((result.stderr or result.stdout or "systeemactie mislukt").strip())
def install():
    """Installeren met vergrendeling: nooit twee updates tegelijk."""
    _cleanup_stale_stage()
    lock=acquire_lock()
    if lock is None:
        emit("status",message="Er draait al een Aurora-update; deze dubbele uitvoer wordt overgeslagen.")
        emit("result",ok=False,error="Er draait al een Aurora-update-installatie; dubbele uitvoer overgeslagen.")
        return 1
    try:
        return _install()
    finally:
        release_lock(lock)

def _install():
    manifest_url=resolve_manifest_url(); data=manifest(); old=installed(); files,system_files=data.get("files",[]),data.get("system_files",[]); missing,system_missing=missing_packages(data,"required_packages"),missing_packages(data,"system_packages"); policy=data.get("system_policy",{}); do_refresh=bool(system_missing or system_files); actions=int(do_refresh and policy.get("refresh_xbps_metadata"))+int(do_refresh and policy.get("refresh_certificates"))
    if old.get("version") and version_key(data["version"])<version_key(old["version"]):
        emit("status",message=f"Downgrade geweigerd: bron {data['version']} is ouder dan de geïnstalleerde versie {old['version']}.")
        emit("result",ok=False,error=f"Downgrade geweigerd: bronversie {data['version']} is ouder dan de geïnstalleerde versie {old['version']}.",update_available=False)
        return 1
    unchanged=old.get("version")==data["version"] and not any(item_changed(i) for i in files) and not any(item_changed(i,True) for i in system_files) and not missing and not system_missing
    if unchanged:
        emit("status",message=f"Aurora is up-to-date — versie {data['version']} is al geïnstalleerd; dubbele installatie overgeslagen"); emit("result",ok=True,version=data["version"],release_name=data.get("release_name",data["version"]),installed_at=old.get("installed_at"),history=old.get("history",[]),update_available=False,already_installed=True); return 0
    total,done,stamp=len(files)+len(system_files)+bool(missing or system_missing)+actions,0,time.strftime("%Y%m%d-%H%M%S")
    STATE.mkdir(parents=True,exist_ok=True); BACKUPS.mkdir(parents=True,exist_ok=True); stage,backup_dir=Path(tempfile.mkdtemp(prefix="aurora-update-",dir=STATE.parent)),BACKUPS/stamp
    try:
        emit("status",message="Manifest opgehaald en gecontroleerd"); base=manifest_url.rsplit("/",1)[0]
        for item in files+system_files:
            source=item["source"].lstrip("/"); raw=fetch(f"{REPO}/{source}" if manifest_url.startswith(REPO) else f"{base}/{source}")
            if hashlib.sha256(raw).hexdigest()!=item["sha256"]:raise ValueError(f"SHA-256-controle mislukt voor {source}")
            out=stage/source; out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes(raw); done+=1; emit("progress",fraction=done/max(total,1),message=f"Gedownload: {source}")
        if missing or system_missing:
            emit("status",message="Aurora-afhankelijkheden installeren via XBPS"); run_root(["xbps-install","-y",*(missing+system_missing)]); done+=1; emit("progress",fraction=done/max(total,1),message="Afhankelijkheden geïnstalleerd")
        if do_refresh and policy.get("refresh_xbps_metadata"):
            emit("status",message="XBPS-repositorymetadata verversen"); run_root(["xbps-install","-S"]); done+=1; emit("progress",fraction=done/max(total,1),message="XBPS-metadata vernieuwd")
        if do_refresh and policy.get("refresh_certificates"):
            emit("status",message="Systeemcertificaten verversen"); run_root(["xbps-reconfigure","-f","ca-certificates"]); done+=1; emit("progress",fraction=done/max(total,1),message="Certificaten vernieuwd")
        backup_dir.mkdir(parents=True,exist_ok=True)
        for item in files:
            target,source=target_path(item["target"]),stage/item["source"].lstrip("/")
            if target.exists():shutil.copy2(target,backup_dir/target.as_posix().lstrip("/").replace("/","__"))
            target.parent.mkdir(parents=True,exist_ok=True); os.replace(source,target); os.chmod(target,int(item.get("mode","644"),8))
        for item in system_files:
            target,source=system_target_path(item["target"]),stage/item["source"].lstrip("/"); run_root(["install","-D","-m",item.get("mode","644"),str(source),str(target)])
        history=[entry for entry in old.get("history",[]) if entry.get("version")!=data["version"]]; history.append({"version":data["version"],"release_name":data.get("release_name",data["version"]),"installed_at":stamp,"manifest":manifest_url}); history=history[-20:]
        record={"version":data["version"],"release_name":data.get("release_name",data["version"]),"installed_at":stamp,"manifest":manifest_url,"history":history}; INSTALLED.write_text(json.dumps(record,indent=2),encoding="utf-8"); HISTORY.write_text(json.dumps(history,indent=2),encoding="utf-8"); emit("progress",fraction=1.0,message="Aurora-update voltooid"); pruned=_prune_backups(); emit("status",message=(f"Opruiming voltooid: {pruned} oude reservekopie(ën) verwijderd." if pruned else "Opruiming voltooid: geen oude reservekopieën.")); emit("result",ok=True,version=data["version"],release_name=data.get("release_name",data["version"]),installed_at=stamp,history=history,backup=str(backup_dir),update_available=False); notify("Aurora is bijgewerkt",f"Aurora {data.get('release_name',data['version'])} ({data['version']}) is geïnstalleerd."); return 0
    except Exception as exc: emit("result",ok=False,error=str(exc),backup=str(backup_dir) if backup_dir.exists() else None); notify("Aurora-update mislukt",str(exc)); return 1
    finally: shutil.rmtree(stage,ignore_errors=True)
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--check",action="store_true"); parser.add_argument("--install",action="store_true"); args=parser.parse_args()
    if args.check==args.install:parser.error("gebruik --check of --install")
    try:return check() if args.check else install()
    except Exception as exc:emit("result",ok=False,error=str(exc)); return 1
if __name__=="__main__":raise SystemExit(main())
def build_card():
    """Build the Aurora-only updater card used by Aurora Settings."""
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib, GdkPixbuf
    frame = Gtk.Frame(); frame.add_css_class("card"); frame.set_margin_bottom(12)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    for side in (16,):
        outer.set_margin_start(side); outer.set_margin_end(side)
    outer.set_margin_top(14); outer.set_margin_bottom(14)

    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
    logo_path = "/usr/share/aurora/logo.png"
    if os.path.exists(logo_path):
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(logo_path, 56, 56, True)
            logo = Gtk.Image.new_from_pixbuf(pix)
        except Exception:
            logo = Gtk.Image.new_from_icon_name("aurora")
    else:
        logo = Gtk.Image.new_from_icon_name("aurora")
    logo.set_pixel_size(56); logo.set_tooltip_text("Aurora OS"); header.append(logo)
    head_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    head_txt.set_hexpand(True)
    title = Gtk.Label(label="<b>Aurora Updates</b>", use_markup=True, xalign=0.0); title.add_css_class("title-3"); head_txt.append(title)
    build_info = Gtk.Label(label="Build 2026.09.10.17 · GitHub-bron · update-vergrendeling · geen downgrades", xalign=0.0); build_info.add_css_class("dim-label"); head_txt.append(build_info)
    header.append(head_txt)
    outer.append(header)

    status = Gtk.Label(label="Nog niet gecontroleerd.", wrap=True, selectable=True, xalign=0.0)
    outer.append(status)

    def set_status(text, kind=None):
        for css in ("success", "warning", "error"):
            status.remove_css_class(css)
        if kind:
            status.add_css_class(kind)
        status.set_text(text)

    previous = installed()
    installed_info = Gtk.Label(label="Geïnstalleerde versie: nog niet vastgesteld", wrap=True, xalign=0.0)
    installed_info.add_css_class("dim-label")
    if previous.get("version"):
        installed_info.set_text(f"Geïnstalleerd: {previous.get('release_name', previous.get('version'))} · versie {previous.get('version')} · {previous.get('installed_at', 'onbekende datum')}")
    outer.append(installed_info)

    progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    progress = Gtk.ProgressBar(); progress.set_show_text(True); progress.set_fraction(0); progress.set_text("Wachten"); progress.set_hexpand(True)
    elapsed_lbl = Gtk.Label(label=""); elapsed_lbl.add_css_class("monospace"); elapsed_lbl.add_css_class("dim-label")
    progress_row.append(progress); progress_row.append(elapsed_lbl)
    outer.append(progress_row)

    notes = Gtk.Label(label="", wrap=True, selectable=True, xalign=0.0); notes.add_css_class("dim-label")
    outer.append(notes)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    check_button = Gtk.Button(label="Controleren"); check_button.set_icon_name("view-refresh-symbolic")
    install_button = Gtk.Button(label="Aurora bijwerken"); install_button.set_icon_name("software-update-available-symbolic"); install_button.add_css_class("suggested-action")
    stop_button = Gtk.Button(label="Stoppen"); stop_button.set_sensitive(False)
    install_button.set_sensitive(False)
    buttons.append(check_button); buttons.append(install_button); buttons.append(stop_button)
    outer.append(buttons)

    running = [False]
    process_ref = [None]
    watchdog_id = [None]
    timer_id = [None]
    run_start = [0.0]

    def start_timer():
        run_start[0] = time.monotonic()
        elapsed_lbl.set_text("0 s")
        def tick():
            elapsed_lbl.set_text(f"{int(time.monotonic() - run_start[0])} s")
            return True
        timer_id[0] = GLib.timeout_add(1000, tick)

    def stop_timer():
        if timer_id[0] is not None:
            GLib.source_remove(timer_id[0])
            timer_id[0] = None
        elapsed_lbl.set_text("")

    def handle(event):
        if event.get("event") == "status":
            set_status(event.get("message", "Bezig…"))
        elif event.get("event") == "progress":
            fraction = max(0.0, min(1.0, float(event.get("fraction", 0))))
            progress.set_fraction(fraction)
            progress.set_text(f"{fraction:.0%} · {event.get('message', 'Bezig…')}")
        elif event.get("event") == "result":
            running[0] = False
            check_button.set_sensitive(True)
            stop_button.set_sensitive(False)
            if watchdog_id[0] is not None:
                GLib.source_remove(watchdog_id[0]); watchdog_id[0] = None
            stop_timer()
            progress.set_fraction(1.0)
            if event.get("ok"):
                if event.get("installed_at"):
                    installed_info.set_text(f"Geïnstalleerd: {event.get('release_name', event.get('version'))} · versie {event.get('version')} · {event.get('installed_at')}")
                files = event.get("files", []) + event.get("system_files", [])
                packages = event.get("packages", []) + event.get("system_packages", [])
                if event.get("update_available"):
                    details = []
                    if files: details.append(f"{len(files)} bestand(en)")
                    if packages: details.append(f"{len(packages)} pakket(pen)")
                    set_status(f"{event.get('release_name', 'Aurora')} ({event.get('version')}) is beschikbaar" + (f" — {', '.join(details)}." if details else "."), "warning")
                    installed_info.set_text(f"Geïnstalleerd: {event.get('previous') or 'onbekend'} → Beschikbaar: {event.get('version')}")
                    install_button.set_sensitive(True)
                elif event.get("downgrade"):
                    set_status(f"De bron ({event.get('version')}) is ouder dan de geïnstalleerde versie ({event.get('previous')}); er wordt geen downgrade aangeboden.", "success")
                    install_button.set_sensitive(False)
                else:
                    set_status(f"Aurora {event.get('version', 'componenten')} is up-to-date — er is geen nieuwe Aurora-update.", "success")
                    install_button.set_sensitive(False)
                progress.set_text("Voltooid")
                notes.set_text(event.get("release_notes", ""))
            else:
                progress.set_text("Mislukt")
                set_status("Aurora-update mislukt: " + str(event.get("error", "onbekende fout")), "error")
                install_button.set_sensitive(False)

    def run(mode):
        if running[0]: return
        running[0] = True
        check_button.set_sensitive(False)
        stop_button.set_sensitive(mode == "--check")
        install_button.set_sensitive(False)
        set_status("Controle wordt gestart…")
        progress.set_fraction(0.0); progress.set_text("Bezig…")
        start_timer()
        try:
            proc = subprocess.Popen([sys.executable, "-u", __file__, mode], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as exc:
            running[0] = False
            check_button.set_sensitive(True); stop_button.set_sensitive(False); stop_timer()
            progress.set_fraction(0); progress.set_text("Niet gestart")
            set_status(f"Updater kon niet starten: {exc}", "error")
            return
        process_ref[0] = proc
        # Een controle mag maximaal 30 s stil staan; een installatie wacht ook op
        # de polkit-bevestiging en krijgt daarom een ruime stilstandsgrens.
        watchdog_seconds = 30 if mode == "--check" else 600
        def watchdog():
            if running[0] and proc.poll() is None:
                proc.kill()
                running[0] = False
                check_button.set_sensitive(True); stop_button.set_sensitive(False)
                install_button.set_sensitive(False)
                message = ("De Aurora-repository gaf binnen 30 seconden geen antwoord. Controleer internet, DNS of GitHub." if mode == "--check"
                           else "De installatie gaf te lang geen voortgang; mogelijk wachtte polkit op bevestiging. Probeer opnieuw.")
                handle({"event": "result", "ok": False, "error": message})
                return False
            return False
        watchdog_id[0] = GLib.timeout_add_seconds(watchdog_seconds, watchdog)
        def read():
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                    GLib.idle_add(handle, event)
                except ValueError:
                    pass
            rc = proc.wait()
            def done():
                running[0] = False; process_ref[0] = None
                if watchdog_id[0] is not None:
                    GLib.source_remove(watchdog_id[0]); watchdog_id[0] = None
                stop_timer()
                check_button.set_sensitive(True); stop_button.set_sensitive(False)
                if mode == "--install" and rc == 0:
                    install_button.set_sensitive(False)
                return False
            GLib.idle_add(done)
        threading.Thread(target=read, daemon=True).start()

    def stop_run(_button):
        proc = process_ref[0]
        if running[0] and proc is not None and proc.poll() is None:
            proc.kill(); running[0] = False; stop_button.set_sensitive(False); check_button.set_sensitive(True); install_button.set_sensitive(False)
            stop_timer()
            progress.set_fraction(0); progress.set_text("Gestopt")
            set_status("Controle gestopt. Je kunt opnieuw controleren.")

    check_button.connect("clicked", lambda *_: run("--check"))
    install_button.connect("clicked", lambda *_: run("--install"))
    stop_button.connect("clicked", stop_run)
    frame.set_child(outer)
    return frame

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); parser.add_argument("--install", action="store_true"); args = parser.parse_args()
    if args.check == args.install: parser.error("gebruik --check of --install")
    try: return check() if args.check else install()
    except Exception as exc: emit("result", ok=False, error=str(exc)); return 1

if __name__ == "__main__": raise SystemExit(main())
