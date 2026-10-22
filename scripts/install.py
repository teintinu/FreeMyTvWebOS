#!/usr/bin/env python3
"""
Free My TV for webOS - One-Command Installer
Automates discovery, pairing, and installation onto your LG Smart TV
"""

import os
import sys
import json
import socket
import shlex
import time
import uuid
import urllib.request
import subprocess
import shutil

# Windows' default console codepage can't encode the emoji in the success
# message below; UTF-8 output works on every platform this script runs on.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO_DIR, "app", "appinfo.json"), "r", encoding="utf-8") as _f:
    _appinfo = json.load(_f)
APP_ID = _appinfo["id"]
DIST_IPK = os.path.join(REPO_DIR, "dist", f"{APP_ID}_{_appinfo['version']}_all.ipk")

def discover_lg_tv():
    print("[*] Scanning local network for LG webOS TV...")
    msg = (
        'M-SEARCH * HTTP/1.1\r\n'
        'HOST: 239.255.255.250:1900\r\n'
        'MAN: "ssdp:discover"\r\n'
        'MX: 2\r\n'
        'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n'
    ).encode('utf-8')
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(2.5)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.sendto(msg, ('239.255.255.250', 1900))
        while True:
            data, addr = sock.recvfrom(4096)
            ip = addr[0]
            # Probe dev port 9922
            s = socket.socket()
            s.settimeout(0.3)
            if s.connect_ex((ip, 9922)) == 0:
                s.close()
                sock.close()
                return ip
            s.close()
    except Exception:
        pass
    sock.close()
    return None

def install_app(tv_ip, passphrase=None):
    if not os.path.exists(DIST_IPK):
        print("[!] Package not found. Building first...")
        build_script = os.path.join(REPO_DIR, "scripts", "build.py")
        subprocess.run(f"python {build_script}", shell=True)

    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    raw_key = os.path.join(ssh_dir, "webos_tv_rsa")
    dec_key = os.path.join(ssh_dir, "webos_tv_rsa_decrypted")

    # Download key from TV if not present
    if not os.path.exists(dec_key):
        if not passphrase:
            passphrase = input(f"Enter the 6-character Passphrase shown in Developer Mode on TV ({tv_ip}): ").strip()

        print(f"[*] Downloading authentication key from http://{tv_ip}:9991/webos_rsa ...")
        try:
            with urllib.request.urlopen(f"http://{tv_ip}:9991/webos_rsa", timeout=5) as resp:
                key_data = resp.read()
                with open(raw_key, "wb") as f:
                    f.write(key_data)
        except Exception as e:
            print(f"[!] Failed to fetch key: {e}")
            print("[!] Make sure 'Key Server' is toggled ON in the Developer Mode app on the TV.")
            return False

        # Decrypt key
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(key_data, password=passphrase.encode())
        unenc = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        )
        with open(dec_key, "wb") as f:
            f.write(unenc)
        print("[+] SSH Key saved & decrypted successfully.")

    print(f"[*] Uploading Free My TV to TV ({tv_ip})...")
    # List-form args (no shell=True) throughout: on Windows, shell=True runs
    # the command through cmd.exe regardless of which shell launched Python,
    # and cmd.exe mangles the embedded double quotes in the JSON payloads
    # below before ssh.exe ever sees them. Passing argv as a list sidesteps
    # every local shell's quoting rules; the single remote command string
    # still reaches the TV's real POSIX shell exactly as written.
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa"]
    scp_args = ["scp", *ssh_opts, "-P", "9922", "-i", dec_key, DIST_IPK, f"prisoner@{tv_ip}:/media/developer/temp/app.ipk"]
    res = subprocess.run(scp_args)
    if res.returncode != 0:
        print("[!] SCP upload failed.")
        return False

    target = f"prisoner@{tv_ip}"

    # Two firmware quirks make the obvious `ssh ... luna-send-pub ...` approach
    # unreliable. First, this sshd rejects forced PTY allocation, and without a
    # TTY luna-send-pub loses its replies when the channel closes as soon as
    # the client exits. Second, dev/install streams ~15-20 progress events
    # before the final "installed" status, so the answer arrives long after the
    # first byte. Running the call detached on the TV with its output sent to a
    # file, then polling that file over fresh SSH sessions, sidesteps both and
    # removes the need for `timeout` — the busybox build on webOS only accepts
    # `timeout -t SECS`, which is what broke earlier attempts.
    def run_luna(remote_cmd, success_marker, failure_markers=('"state":"failed"', '"errorText"'), timeout=120):
        log_path = "/tmp/freemytv-" + uuid.uuid4().hex[:8] + ".log"
        start = "nohup sh -c %s >/dev/null 2>&1 & echo started" % shlex.quote(
            "%s > %s 2>&1" % (remote_cmd, log_path))
        subprocess.run(["ssh", *ssh_opts, "-p", "9922", "-i", dec_key, target, start],
                       capture_output=True, text=True)
        output = ""
        ok = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            res = subprocess.run(["ssh", *ssh_opts, "-p", "9922", "-i", dec_key, target,
                                  "cat %s 2>/dev/null" % log_path],
                                 capture_output=True, text=True)
            output = res.stdout or output
            if success_marker in output:
                ok = True
                break
            if any(marker in output for marker in failure_markers):
                break
        subprocess.run(["ssh", *ssh_opts, "-p", "9922", "-i", dec_key, target,
                        "rm -f %s" % log_path], capture_output=True, text=True)
        return ok, output

    print("[*] Installing package on TV...")
    install_payload = json.dumps({"id": APP_ID, "ipkUrl": "/media/developer/temp/app.ipk", "subscribe": True})
    install_remote_cmd = f"luna-send-pub -n 30 luna://com.webos.appInstallService/dev/install '{install_payload}'"
    installed, install_out = run_luna(install_remote_cmd, '"state":"installed"')
    if not installed:
        print("[!] Install did not reach the 'installed' state. Raw output:")
        print(install_out or "(empty)")
        return False

    print("[*] Launching Free My TV...")
    launch_payload = json.dumps({"id": APP_ID})
    launch_remote_cmd = f"luna-send-pub -n 1 luna://com.webos.applicationManager/launch '{launch_payload}'"
    launched, launch_out = run_luna(launch_remote_cmd, '"returnValue":true')
    if not launched:
        print("[!] Install succeeded but launch failed. Raw output:")
        print(launch_out or "(empty)")
        return False

    print("\n[+] SUCCESS! Free My TV is installed and running on your LG TV! Enjoy! 🎉")
    return True

if __name__ == "__main__":
    ip = sys.argv[1] if len(sys.argv) > 1 else None
    passphrase = sys.argv[2] if len(sys.argv) > 2 else None

    if not ip:
        ip = discover_lg_tv()
        if not ip:
            ip = input("Enter TV IP Address (shown in Developer Mode app): ").strip()

    install_app(ip, passphrase)
