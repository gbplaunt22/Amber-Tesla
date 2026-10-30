#!/usr/bin/python3
"""Install service definitions and defaults; model/package provisioning is separate."""
import hashlib,json,os,pathlib,secrets,shutil,subprocess

ROOT=pathlib.Path("/backingfiles/amber")
APP=ROOT/"app"
CONFIG=pathlib.Path("/mutable/amber")

def run(*args):return subprocess.run(args,check=True,text=True,capture_output=True).stdout

def main():
    if os.geteuid()!=0:raise SystemExit("Run with sudo")
    for protected in (APP,ROOT/"models",ROOT/"llama",ROOT/"venv"):
        if protected.exists():
            run("chown","-R","root:root",str(protected))
            run("chmod","-R","go-w",str(protected))
    CONFIG.mkdir(parents=True,exist_ok=True)
    # Pi firmware disables memory accounting by default. Enable the downstream
    # kernel override, preserving all existing boot parameters. Requires reboot.
    cmdline=pathlib.Path('/boot/firmware/cmdline.txt')
    if cmdline.exists() and 'cgroup_enable=memory' not in cmdline.read_text().split():
        backup_cmdline=CONFIG/'cmdline.txt.before'
        if not backup_cmdline.exists():shutil.copy2(cmdline,backup_cmdline)
        cmdline.write_text(cmdline.read_text().strip()+' cgroup_enable=memory\n')
    auth=CONFIG/"auth.json"
    connection=CONFIG/"connection.json"
    if not auth.exists():
        password=secrets.token_urlsafe(12);salt=secrets.token_bytes(16)
        auth.write_text(json.dumps({"username":"gavin","salt":salt.hex(),"password_hash":hashlib.pbkdf2_hmac("sha256",password.encode(),salt,200000).hex(),"secret":secrets.token_hex(32)}))
        connection.write_text(json.dumps({"dashboard_username":"gavin","dashboard_password":password},indent=2))
    network=CONFIG/"network.json"
    if not network.exists():network.write_text(json.dumps({"ssid":"AMBER-Local","password":secrets.token_urlsafe(12)},indent=2))
    run("chown","root:amber",str(auth));auth.chmod(0o640)
    network.chmod(0o600);connection.chmod(0o600)
    (ROOT/"data").mkdir(exist_ok=True)
    run("chown","-R","amber:amber",str(ROOT/"data"))
    roles={"ingest":("256M","50%","AF_UNIX"),"video":("1536M","250%","AF_UNIX"),"parser":("1G","100%","AF_UNIX AF_INET AF_INET6"),"web":("256M","50%","AF_UNIX AF_INET AF_INET6")}
    for role,(memory,cpu,families) in roles.items():
        allow="IPAddressAllow=localhost\n" if role in ("parser","web") else ""
        if role=="web":allow+="IPAddressAllow=192.168.66.0/24\n"
        isolation="SystemCallFilter=~connect\nSystemCallErrorNumber=EPERM\n" if role=="web" else "PrivateNetwork=yes\n"
        unit=f"""[Unit]
Description=Amber Local {role}
After=backingfiles.mount mutable.mount
RequiresMountsFor=/backingfiles/amber /mutable
StartLimitIntervalSec=120
StartLimitBurst=10

[Service]
Type=simple
User=amber
Group=amber
WorkingDirectory={APP}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=HF_HUB_OFFLINE=1
Environment=OMP_NUM_THREADS=1
ExecStart={ROOT}/venv/bin/python -m amber.cli {role}
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
Nice={15 if role=='parser' else 10}
IOSchedulingClass=idle
MemoryMax={memory}
CPUQuota={cpu}
TasksMax=64
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={ROOT}/data
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies={families}
IPAddressDeny=any
{allow}{isolation}UMask=0077
StandardOutput=journal
StandardError=journal
LogRateLimitIntervalSec=60
LogRateLimitBurst=30

[Install]
WantedBy=multi-user.target
"""
        pathlib.Path(f"/etc/systemd/system/amber-{role}.service").write_text(unit)
    pathlib.Path("/etc/systemd/system/amber-ap.service").write_text(f"""[Unit]
Description=Amber Local Wi-Fi access point
After=NetworkManager.service mutable.mount
Wants=NetworkManager.service
RequiresMountsFor=/backingfiles/amber /mutable
[Service]
ExecStart=/usr/bin/python3 {APP}/deploy/access_point.py
ExecStop=-/usr/bin/nmcli connection down amber-local
ExecStopPost=-/usr/sbin/iw dev amber0 del
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
""")
    pathlib.Path("/etc/systemd/system/amber-dhcp.service").write_text("""[Unit]
Description=Amber Local AP address assignment (no DNS or internet routing)
After=amber-ap.service
Wants=amber-ap.service
[Service]
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/dev/null --interface=amber0 --bind-dynamic --port=0 --dhcp-range=192.168.66.20,192.168.66.80,255.255.255.0,12h --dhcp-option=3 --dhcp-option=6 --dhcp-leasefile=/run/amber-dhcp.leases --log-facility=-
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
""")
    # Update only the requested setting; keep existing TeslaUSB variables intact.
    setup=pathlib.Path("/root/teslausb_setup_variables.conf")
    backup=CONFIG/"teslausb_setup_variables.conf.before"
    if not backup.exists():shutil.copy2(setup,backup);backup.chmod(0o600)
    import re
    content=setup.read_text()
    content=re.sub(r"(?m)^\s*(?:export\s+)?SNAPSHOT_INTERVAL=.*$","export SNAPSHOT_INTERVAL=60",content)
    if not re.search(r"(?m)^export SNAPSHOT_INTERVAL=60$",content):content+="\n# Amber Local: scan completed recordings during drives.\nexport SNAPSHOT_INTERVAL=60\n"
    setup.write_text(content)
    run("systemctl","daemon-reload")
    run("systemctl","enable",*[f"amber-{r}.service" for r in (*roles,"ap","dhcp")])
    run("systemctl","start",*[f"amber-{r}.service" for r in roles])
    # Networking activation is deliberately last, after transferring all artifacts.
    print("Services installed. Start amber-ap and amber-dhcp after model provisioning.")
    print("Snapshot interval configured. Restart TeslaUSB only when USB state is not attached.")

if __name__=="__main__":main()
