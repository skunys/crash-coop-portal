#!/usr/bin/env python3
"""Is the portal reachable from the internet? Prints the current public IP,
then asks an outside service (check-host.net) to open TCP 8080 and UDP 8189."""
import json
import sys
import time
import urllib.request

PORT_TCP = 8080
PORT_UDP = 8189


def api(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "curl/8"})
    return json.load(urllib.request.urlopen(req, timeout=25))


def public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            ip = urllib.request.urlopen(url, timeout=8).read().decode().strip()
            if ip.count(".") == 3:
                return ip
        except Exception:
            pass
    return None


def check(kind, host):
    try:
        start = api(f"https://check-host.net/check-{kind}?host={host}&max_nodes=2")
        time.sleep(13)
        res = api("https://check-host.net/check-result/" + start["request_id"])
    except Exception as exc:
        print(f"  {kind.upper()} check could not run: {exc}")
        return None
    ok = False
    for node, out in res.items():
        value = out
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, dict) and value.get("error"):
            print(f"  {kind.upper():3s} {node:24s} FAILED: {value['error']}")
        else:
            ok = True
            print(f"  {kind.upper():3s} {node:24s} reachable {value}")
    return ok


def main() -> int:
    ip = public_ip()
    if not ip:
        print("Could not determine the public IP (no internet?)")
        return 2
    print(f"public IP: {ip}")
    print(f"page:      http://{ip}:{PORT_TCP}/?k=<token from the portal window>")
    print("checking from outside:")
    ok_tcp = check("tcp", f"{ip}:{PORT_TCP}")
    ok_udp = check("udp", f"{ip}:{PORT_UDP}")
    print()
    if ok_tcp and ok_udp:
        print("RESULT: OPEN -- the link works for your friend now.")
        return 0
    print("RESULT: BLOCKED -- the router is not forwarding yet.")
    print("  Router (192.168.2.1) -> Forward Rules -> IPv4 Port Mapping:")
    print(f"    TCP {PORT_TCP} -> 192.168.2.101:{PORT_TCP}")
    print(f"    UDP {PORT_UDP} -> 192.168.2.101:{PORT_UDP}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
