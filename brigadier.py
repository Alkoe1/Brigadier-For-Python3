#!/usr/bin/env python3

import os
import sys
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import plistlib
import re
import tempfile
import shutil
import optparse
import datetime
import platform

try:
    import requests # pyright: ignore[reportMissingModuleSource]
except ImportError:
    requests = None

from pprint import pprint
from xml.dom import minidom

VERSION = '0.2.6'
SUCATALOG_URL = 'https://swscan.apple.com/content/catalogs/others/index-11-10.15-10.14-10.13-10.12-10.11-10.10-10.9-mountainlion-lion-snowleopard-leopard.merged-1.sucatalog'
# 7-Zip MSI
SEVENZIP_URL = 'https://www.7-zip.org/a/7z2201-x64.msi'

def status(msg):
    print(f"{msg}\n")

def getCommandOutput(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    return out.decode('utf-8', errors='ignore')

def getMachineModel():
    if platform.system() == 'Windows':
        rawxml = getCommandOutput(['wmic', 'computersystem', 'get', 'model', '/format:RAWXML'])
        if not rawxml.strip():
            return "Unknown"
        dom = minidom.parseString(rawxml)
        nodes = dom.getElementsByTagName("VALUE")
        return nodes[0].firstChild.data if nodes else "Unknown"
    elif platform.system() == 'Darwin':
        plistxml = getCommandOutput(['system_profiler', 'SPHardwareDataType', '-xml'])
        # Python 3'te readPlistFromString yerine loads kullanılır
        plist = plistlib.loads(plistxml.encode('utf-8'))
        return plist[0]['_items'][0]['machine_model']
    return "Unknown"

def downloadFile(url, filename, use_requests=False):
    def reporthook(blocknum, blocksize, totalsize):
        readsofar = blocknum * blocksize
        if totalsize > 0:
            percent = readsofar * 1e2 / totalsize
            console_out = "\r%5.1f%% %*d / %d bytes" % (
                percent, len(str(totalsize)), readsofar, totalsize)
            sys.stderr.write(console_out)
            if readsofar >= totalsize:
                sys.stderr.write("\n")
        else:
            sys.stderr.write(f"read {readsofar}\n")

    if use_requests and requests is not None:
        resp = requests.get(url, stream=True)
        with open(filename, 'wb') as fd:
            for chunk in resp.iter_content(chunk_size=1024):
                fd.write(chunk)
        return
    
    urllib.request.urlretrieve(url, filename, reporthook=reporthook)

def sevenzipExtract(arcfile, command='e', out_dir=None):
    sz_path = os.path.join(os.environ.get('SYSTEMDRIVE', 'C:') + "\\", "Program Files", "7-Zip", "7z.exe")
    cmd = [sz_path, command]
    if not out_dir:
        out_dir = os.path.dirname(arcfile)
    cmd.append("-o" + out_dir)
    cmd.append("-y")
    cmd.append(arcfile)
    status(f"Calling 7-Zip command: {' '.join(cmd)}")
    retcode = subprocess.call(cmd)
    if retcode:
        sys.exit(f"Command failure: {' '.join(cmd)} exited {retcode}.")

def postInstallConfig():
    # Raw string (r"") kullanarak escape sequence hatalarını önledik
    regdata = r"""Windows Registry Editor Version 5.00

[HKEY_CURRENT_USER\Software\Apple Inc.\Apple Keyboard Support]
"FirstTimeRun"=dword:00000000"""
    handle, path = tempfile.mkstemp(suffix=".reg")
    with os.fdopen(handle, 'w') as fd:
        fd.write(regdata)
    subprocess.call(['regedit.exe', '/s', path])
    os.remove(path)

def findBootcampMSI(search_dir):
    candidates = ['BootCamp64.msi', 'BootCamp.msi']
    for root, dirs, files in os.walk(search_dir):
        for msi in candidates:
            if msi in files:
                return os.path.join(root, msi)
    return None

def installBootcamp(msipath):
    if not msipath:
        status("BootCamp MSI not found.")
        return
    logpath = os.path.abspath("C:\\BootCamp_Install.log")
    cmd = ['msiexec', '/i', msipath, '/qb-', '/norestart', '/log', logpath]
    status(f"Executing command: {' '.join(cmd)}")
    subprocess.call(cmd)
    status("Installation triggered. Check log at C:\\BootCamp_Install.log")
    postInstallConfig()
    
def main():
    scriptdir = os.path.abspath(os.path.dirname(sys.argv[0]))

    o = optparse.OptionParser()
    o.add_option('-m', '--model', action="append")
    o.add_option('-i', '--install', action="store_true")
    o.add_option('-o', '--output-dir')
    o.add_option('-k', '--keep-files', action="store_true")
    o.add_option('-p', '--product-id')
    o.add_option('-V', '--version', action="store_true")

    opts, args = o.parse_args()
    if opts.version:
        print(VERSION)
        sys.exit(0)

    if opts.install:
        if platform.system() == 'Darwin':
            sys.exit("Installing Boot Camp can only be done on Windows!")
        if platform.system() == 'Windows' and platform.machine() != 'AMD64':
            sys.exit("Only 64-bit Windows is supported for auto-install.")

    output_dir = opts.output_dir if opts.output_dir else os.getcwd()
    
    models = opts.model if opts.model else [getMachineModel()]
    status(f"Using Mac model: {', '.join(models)}")

    for model in models:
        sucatalog_url = SUCATALOG_URL
        plist_path = os.path.join(scriptdir, 'brigadier.plist')
        if os.path.isfile(plist_path):
            try:
                with open(plist_path, 'rb') as f:
                    config_plist = plistlib.load(f)
                    if 'CatalogURL' in config_plist:
                        sucatalog_url = config_plist['CatalogURL']
            except:
                status("Could not read brigadier.plist.")

        status(f"Fetching catalog from {sucatalog_url}...")
        with urllib.request.urlopen(sucatalog_url) as response:
            data = response.read()
        
        # Python 3 uyumlu plist yükleme
        p = plistlib.loads(data)
        allprods = p.get('Products', {})

        bc_prods = []
        for prod_id, prod_data in allprods.items():
            if 'ServerMetadataURL' in prod_data:
                if 'BootCamp' in prod_data['ServerMetadataURL']:
                    bc_prods.append((prod_id, prod_data))

        pkg_data = []
        re_model = r"([a-zA-Z]{4,12}[1-9]{1,2}\,[1-6])" # Raw string
        for bc_prod in bc_prods:
            dists = bc_prod[1].get('Distributions', {})
            if 'English' in dists:
                disturl = dists['English']
                with urllib.request.urlopen(disturl) as dist_resp:
                    dist_data = dist_resp.read().decode('utf-8', errors='ignore')
                
                if re.search(model, dist_data):
                    pkg_data.append({bc_prod[0]: bc_prod[1]})

        if not pkg_data:
            sys.exit(f"No Boot Camp ESD found for model {model}.")

        # Seçim mantığı
        if len(pkg_data) > 1:
            latest_date = datetime.datetime.min
            chosen_product = None
            for p_dict in pkg_data:
                pid = list(p_dict.keys())[0]
                pdate = p_dict[pid].get('PostDate')
                # Date objesi gelirse karşılaştır
                if pdate and pdate.replace(tzinfo=None) > latest_date:
                    latest_date = pdate.replace(tzinfo=None)
                    chosen_product = pid
            
            if opts.product_id:
                chosen_product = opts.product_id
            
            selected_pkg = [x for x in pkg_data if list(x.keys())[0] == chosen_product][0]
            pkg_data = selected_pkg
        else:
            pkg_data = pkg_data[0]

        pkg_id = list(pkg_data.keys())[0]
        pkg_url = pkg_data[pkg_id]['Packages'][0]['URL']

        landing_dir = os.path.join(output_dir, 'BootCamp-' + pkg_id)
        if os.path.exists(landing_dir):
            shutil.rmtree(landing_dir, ignore_errors=True)

        os.makedirs(landing_dir, exist_ok=True)
        arc_workdir = tempfile.mkdtemp(prefix="bootcamp-unpack_")
        pkg_dl_path = os.path.join(arc_workdir, pkg_url.split('/')[-1])

        status(f"Downloading Boot Camp {pkg_id}...")
        downloadFile(pkg_url, pkg_dl_path)

        if platform.system() == 'Windows':
            sz_binary = os.path.join(os.environ.get('SYSTEMDRIVE', 'C:') + "\\", 'Program Files', '7-Zip', '7z.exe')
            if not os.path.exists(sz_binary):
                status("7-Zip not found, downloading...")
                sz_temp = os.path.join(tempfile.gettempdir(), "7z.msi")
                downloadFile(SEVENZIP_URL, sz_temp, use_requests=True)
                subprocess.call(['msiexec', '/qn', '/i', sz_temp])
            
            status("Extracting files...")
            sevenzipExtract(pkg_dl_path, out_dir=arc_workdir)
            
            payloads = ['Payload', 'Payload~']
            for p in payloads:
                p_path = os.path.join(arc_workdir, p)
                if os.path.exists(p_path):
                    sevenzipExtract(p_path, out_dir=arc_workdir)
            
            dmg_path = os.path.join(arc_workdir, 'WindowsSupport.dmg')
            if os.path.exists(dmg_path):
                sevenzipExtract(dmg_path, command='x', out_dir=landing_dir)
            
            if opts.install:
                installBootcamp(findBootcampMSI(landing_dir))
                if not opts.keep_files:
                    shutil.rmtree(landing_dir, ignore_errors=True)
            
            shutil.rmtree(arc_workdir, ignore_errors=True)

        elif platform.system() == 'Darwin':
            status("Expanding package on macOS...")
            pkg_expand = os.path.join(arc_workdir, 'pkg')
            subprocess.call(['pkgutil', '--expand', pkg_dl_path, pkg_expand])
            subprocess.call(['tar', '-xz', '-C', arc_workdir, '-f', os.path.join(pkg_expand, 'Payload')])
            
            src_dmg = os.path.join(arc_workdir, 'Library/Application Support/BootCamp/WindowsSupport.dmg')
            shutil.move(src_dmg, os.path.join(landing_dir, 'WindowsSupport.dmg'))
            status(f"Extracted to {landing_dir}/WindowsSupport.dmg")
            shutil.rmtree(arc_workdir)

    status("Done.")

if __name__ == "__main__":
    main()
