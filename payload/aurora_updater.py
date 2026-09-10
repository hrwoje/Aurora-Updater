#!/usr/bin/python3
"""Aurora component updater with hash checks and a small, fixed system layer."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
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
    with urllib.request.urlopen(req,timeout=25) as response: return response.read()
def manifest():
    data=json.loads(fetch(MANIFEST_URL).decode())
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
    packages,system_packages=missing_packages(data,"required_packages"),missing_packages(data,"system_packages"); policy=data.get("system_policy",{}); actions=bool(system_files or system_packages) and bool(policy.get("refresh_xbps_metadata") or policy.get("refresh_certificates")); same_version=old.get("version")==data["version"]; available=bool(files or system_files or packages or system_packages or actions or (old.get("version") and not same_version))
    emit("result",ok=True,version=data["version"],release_name=data.get("release_name",data["version"]),previous=old.get("version"),installed_at=old.get("installed_at"),history=old.get("history",[]),release_notes=data.get("release_notes",""),files=files,system_files=system_files,packages=packages,system_packages=system_packages,update_available=available); return 0
def run_root(args):
    result=subprocess.run(["pkexec",*args],text=True,capture_output=True)
    if result.returncode:raise RuntimeError((result.stderr or result.stdout or "systeemactie mislukt").strip())
def install():
    data=manifest(); old=installed(); files,system_files=data.get("files",[]),data.get("system_files",[]); missing,system_missing=missing_packages(data,"required_packages"),missing_packages(data,"system_packages"); policy=data.get("system_policy",{}); do_refresh=bool(system_missing or system_files); actions=int(do_refresh and policy.get("refresh_xbps_metadata"))+int(do_refresh and policy.get("refresh_certificates")); unchanged=old.get("version")==data["version"] and not any(item_changed(i) for i in files) and not any(item_changed(i,True) for i in system_files) and not missing and not system_missing
    if unchanged:
        emit("status",message=f"Versie {data['version']} is al geïnstalleerd; dubbele installatie overgeslagen"); emit("result",ok=True,version=data["version"],release_name=data.get("release_name",data["version"]),installed_at=old.get("installed_at"),history=old.get("history",[]),update_available=False,already_installed=True); return 0
    total,done,stamp=len(files)+len(system_files)+bool(missing or system_missing)+actions,0,time.strftime("%Y%m%d-%H%M%S")
    STATE.mkdir(parents=True,exist_ok=True); BACKUPS.mkdir(parents=True,exist_ok=True); stage,backup_dir=Path(tempfile.mkdtemp(prefix="aurora-update-",dir=STATE.parent)),BACKUPS/stamp
    try:
        emit("status",message="Manifest opgehaald en gecontroleerd"); base=MANIFEST_URL.rsplit("/",1)[0]
        for item in files+system_files:
            source=item["source"].lstrip("/"); raw=fetch(f"{REPO}/{source}" if MANIFEST_URL.startswith(REPO) else f"{base}/{source}")
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
        history=[entry for entry in old.get("history",[]) if entry.get("version")!=data["version"]]; history.append({"version":data["version"],"release_name":data.get("release_name",data["version"]),"installed_at":stamp,"manifest":MANIFEST_URL}); history=history[-20:]
        record={"version":data["version"],"release_name":data.get("release_name",data["version"]),"installed_at":stamp,"manifest":MANIFEST_URL,"history":history}; INSTALLED.write_text(json.dumps(record,indent=2),encoding="utf-8"); HISTORY.write_text(json.dumps(history,indent=2),encoding="utf-8"); emit("progress",fraction=1.0,message="Aurora-update voltooid"); emit("result",ok=True,version=data["version"],release_name=data.get("release_name",data["version"]),installed_at=stamp,history=history,backup=str(backup_dir),update_available=False); notify("Aurora is bijgewerkt",f"Aurora {data.get('release_name',data['version'])} ({data['version']}) is geïnstalleerd."); return 0
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
    logo_path = "/usr/share/aurora/logo.png"
    if os.path.exists(logo_path):
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(logo_path, 72, 72, True)
            logo = Gtk.Image.new_from_pixbuf(pix)
        except Exception:
            logo = Gtk.Image.new_from_icon_name("aurora")
    else:
        logo = Gtk.Image.new_from_icon_name("aurora")
    logo.set_pixel_size(72); logo.set_halign(Gtk.Align.CENTER); logo.set_tooltip_text("Aurora OS"); outer.append(logo)
    title = Gtk.Label(label="<b>Aurora Updates</b>", use_markup=True, xalign=0.5); title.add_css_class("title-3"); outer.append(title)
    intro = Gtk.Label(label="Werk Aurora-componenten bij via de beheerde GitHub-repository. Void- en Flatpak-updates blijven in hun eigen beheerpagina.", wrap=True, xalign=0.0); outer.append(intro)
    status = Gtk.Label(label="Nog niet gecontroleerd.", wrap=True, xalign=0.0); outer.append(status)
    previous = installed()
    previous_text = "Geïnstalleerde versie: nog niet vastgesteld"
    if previous.get("version"):
        previous_text = f"Geïnstalleerd: {previous.get('release_name', previous.get('version'))} · versie {previous.get('version')} · {previous.get('installed_at', 'onbekende datum')}"
    installed_info = Gtk.Label(label=previous_text, wrap=True, xalign=0.0); installed_info.add_css_class("dim-label"); outer.append(installed_info)
    progress = Gtk.ProgressBar(); progress.set_show_text(True); progress.set_fraction(0); progress.set_text("Wachten"); outer.append(progress)
    notes = Gtk.Label(label="", wrap=True, xalign=0.0); notes.add_css_class("dim-label"); outer.append(notes)
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    check_button, install_button = Gtk.Button(label="Controleren"), Gtk.Button(label="Aurora bijwerken")
    install_button.set_sensitive(False); buttons.append(check_button); buttons.append(install_button); outer.append(buttons)
    running = [False]

    def handle(event):
        if event.get("event") == "status": status.set_text(event.get("message", "Bezig…"))
        elif event.get("event") == "progress":
            progress.set_fraction(max(0.0, min(1.0, float(event.get("fraction", 0)))))
            progress.set_text(event.get("message", "Bezig…"))
        elif event.get("event") == "result":
            progress.set_fraction(1.0)
            if event.get("ok"):
                if event.get("installed_at"):
                    installed_info.set_text(f"Geïnstalleerd: {event.get('release_name', event.get('version'))} · versie {event.get('version')} · {event.get('installed_at')}")
                files = event.get("files", []) + event.get("system_files", [])
                packages = event.get("packages", []) + event.get("system_packages", [])
                if event.get("update_available"):
                    status.set_text(f"{event.get('release_name', 'Aurora')} ({event.get('version')}) beschikbaar: {len(files)} bestand(en), {len(packages)} pakket(en).")
                    install_button.set_sensitive(True)
                else:
                    status.set_text(f"Aurora {event.get('version', 'componenten')} is actueel.")
                progress.set_text("Controle voltooid")
                notes.set_text(event.get("release_notes", ""))
            else:
                progress.set_text("Controle mislukt")
                status.set_text("Aurora-update mislukt: " + str(event.get("error", "onbekende fout")))
                install_button.set_sensitive(False)

    def run(mode):
        if running[0]: return
        running[0] = True; check_button.set_sensitive(False); install_button.set_sensitive(False); status.set_text("Controle wordt gestart…"); progress.set_fraction(0.0); progress.set_text("Repositorycontrole bezig…")
        try: proc = subprocess.Popen([sys.executable, "-u", __file__, mode], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED":"1"})
        except OSError as exc:
            running[0] = False
            check_button.set_sensitive(True); progress.set_fraction(0); progress.set_text("Updater kon niet starten"); status.set_text(f"Updater kon niet starten: {exc}"); return
        def read():
            for line in proc.stdout:
                try: event = json.loads(line); GLib.idle_add(handle, event)
                except ValueError: pass
            rc = proc.wait()
            def done():
                running[0] = False; check_button.set_sensitive(True)
                if mode == "--install" and rc == 0: install_button.set_sensitive(False)
                return False
            GLib.idle_add(done)
        threading.Thread(target=read, daemon=True).start()
    check_button.connect("clicked", lambda *_: run("--check")); install_button.connect("clicked", lambda *_: run("--install"))
    frame.set_child(outer); return frame

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); parser.add_argument("--install", action="store_true"); args = parser.parse_args()
    if args.check == args.install: parser.error("gebruik --check of --install")
    try: return check() if args.check else install()
    except Exception as exc: emit("result", ok=False, error=str(exc)); return 1

if __name__ == "__main__": raise SystemExit(main())
