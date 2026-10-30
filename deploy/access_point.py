#!/usr/bin/python3
"""Concurrent AP on the existing radio, following the station's channel."""
import json, pathlib, subprocess, time, signal

NAME="amber-local"
IFACE="amber0"
CONFIG=pathlib.Path("/mutable/amber/network.json")

def run(*args,check=True):
    return subprocess.run(args,check=check,capture_output=True,text=True,timeout=25)

def channel():
    info=run("/usr/sbin/iw","dev","wlan0","info",check=False).stdout
    import re
    found=re.search(r"channel (\d+) \((\d+) MHz\)",info)
    if found:return int(found[1]),"a" if int(found[2])>4900 else "bg"
    return 6,"bg"

def main():
    config=json.loads(CONFIG.read_text())
    previous=None
    while True:
        try:
            if not pathlib.Path("/sys/class/net/"+IFACE).exists():
                run("/usr/sbin/iw","dev","wlan0","interface","add",IFACE,"type","__ap")
                time.sleep(1)
                run("nmcli","device","set",IFACE,"managed","yes")
            exists=run("nmcli","connection","show",NAME,check=False).returncode==0
            if not exists:
                run("nmcli","connection","add","save","no","type","wifi","ifname",IFACE,"con-name",NAME,"ssid",config["ssid"],
                    "802-11-wireless.mode","ap","ipv4.method","manual","ipv4.addresses","192.168.66.1/24",
                    "ipv4.never-default","yes","ipv6.method","disabled","connection.autoconnect","no")
                run("nmcli","connection","modify","--temporary",NAME,"wifi-sec.key-mgmt","wpa-psk","wifi-sec.psk",config["password"])
            chan,band=channel()
            state=run("nmcli","-g","GENERAL.STATE","device","show",IFACE,check=False).stdout
            if previous!=(chan,band) or "100" not in state:
                run("nmcli","connection","down",NAME,check=False)
                run("nmcli","connection","modify","--temporary",NAME,"802-11-wireless.band",band,"802-11-wireless.channel",str(chan))
                run("nmcli","connection","up",NAME,"ifname",IFACE)
                previous=(chan,band)
                print("Local access point ready on channel",chan,flush=True)
        except Exception as e:
            # Never log nmcli arguments: they can contain the WPA password.
            print("Access point retry:",type(e).__name__,flush=True)
        time.sleep(10)

if __name__=="__main__":main()
