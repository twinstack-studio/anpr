# Installing TwinStack Gate at a society

TwinStack Gate runs on a small PC at the gate. Cameras, the guard screen and the barrier talk to it over the society's
own network, so the gate keeps working when the internet is down. With an optional Cloudflare tunnel, management and
residents can also open the app from anywhere.

```
IP camera ──RTSP──▶ gate mini PC (TwinStack Gate + PostgreSQL) ──HTTP──▶ barrier relay ──▶ boom barrier
                         ▲                     │
     guard tablet / PC ──┘  (society Wi-Fi)    └──tunnel──▶ admin and residents' phones (internet)
```

## Hardware

| Item | What to buy | Notes |
|---|---|---|
| Gate PC | Mini PC or small desktop, Intel Core i5/i7 (8th gen or newer) or Ryzen 5/7, 16 GB RAM, 256 GB SSD | Runs Ubuntu 24.04 LTS. No graphics card needed: one camera runs at 3–4 frames per second on an 8-core CPU, enough for cars slowing at a gate. A refurbished Dell OptiPlex / HP EliteDesk mini is fine. For 3+ cameras, a PC with an NVIDIA GPU (install with `ANPR_DEVICE=0`). |
| Camera (one per lane) | 2–4 MP IP camera with RTSP, varifocal lens (2.8–12 mm), IR night vision, IP66 | Any Hikvision / Dahua / Uniview bullet camera works. Mount it 3–6 m before the barrier, 1–1.5 m high, looking at the plate at less than 30° from straight on. Set shutter to 1/500 s or faster if the model allows it (sharper plates at night). |
| Barrier relay | Wi-Fi or Ethernet relay with an HTTP API, e.g. Shelly 1 / Shelly Plus 1 | Wired to the barrier controller's "open" push-button input (dry contact). The electrician connects it in parallel with the existing button, so the guard's remote keeps working. |
| Boom barrier | Existing barrier, or any barrier whose controller has an "open" input | Almost all local barriers have one. |
| Network | PoE switch (4 or 8 port) and Cat6 cable to the camera; Wi-Fi router for the guard tablet | PoE powers the camera over the network cable. |
| Power backup | UPS, 1 kVA or more, for PC, switch, camera and router | Load-shedding: the PC must not switch off. |
| Guard screen | Any Android tablet, old laptop or phone with Chrome | Opens `http://<gate-pc-ip>:8080`. |
| Internet (optional) | Existing society connection or a 4G router | Only needed for remote access and resident phone notifications. |

## Install

1. Install **Ubuntu 24.04 LTS** (Server or Desktop) on the gate PC. Connect it to the same network as the camera and set a
   fixed IP address on the router (DHCP reservation), e.g. `192.168.1.50`.
2. Copy this repository to the PC and run the installer:

   ```bash
   git clone https://github.com/twinstack-studio/anpr.git && cd anpr
   sudo SOC_NAME="Green Valley Residencia" SOC_ADMIN_PASSWORD='a-strong-password' bash deploy/install.sh
   ```

   It installs PostgreSQL, Python packages and the models, creates the `twinstack-gate` service (starts on boot,
   restarts if it crashes) and a nightly database backup in `/var/backups/twinstack-gate` (14 days kept). It takes
   5–15 minutes. Settings are saved in `/etc/twinstack-gate.env`.

   | Setting | Default | |
   |---|---|---|
   | `SOC_NAME` | Our Society | Shown in the app |
   | `SOC_ADMIN_USER` / `SOC_ADMIN_PASSWORD` | admin / (required) | First admin account |
   | `GATE_PORT` | 8080 | Port on the society network |
   | `ANPR_DEVICE` | cpu | `0` on a PC with an NVIDIA GPU |
   | `TUNNEL_TOKEN` | (none) | Cloudflare tunnel token for remote access |

3. Open `http://<gate-pc-ip>:8080` and log in as the admin.

## Set up in the app

1. **Settings:** check the society name and the rules (resident approval, auto barrier, visitors).
2. **Houses and vehicles:** add them one by one or import the society's list as CSV.
3. **Accounts:** create guard accounts, and resident accounts for houses that want the portal.
4. **Cameras:** add each camera with its RTSP address and gate (entry / exit / both):
   - Hikvision: `rtsp://admin:PASSWORD@192.168.1.64:554/Streaming/Channels/101`
   - Dahua: `rtsp://admin:PASSWORD@192.168.1.108:554/cam/realmonitor?channel=1&subtype=0`

   For the barrier, enter the relay's "open" URL, e.g. Shelly: `http://192.168.1.70/relay/0?turn=on&timer=2`
   (switches on for 2 seconds, like a button press). Use **Open barrier** on the Cameras page to test it.
5. Drive a known car through the gate and check that the Gate screen shows it and the barrier opens.

## Remote access (optional)

In the Cloudflare dashboard, create a tunnel for the society (Zero Trust → Networks → Tunnels), add a public hostname
such as `greenvalley-gate.twinstackstudio.com` pointing to `http://localhost:8080`, and pass the tunnel's token to the
installer as `TUNNEL_TOKEN` (or run `sudo cloudflared service install <token>` later). Residents need this address
for phone notifications, which only work over HTTPS.

## Maintenance

- **Update:** `cd anpr && git pull && sudo bash deploy/install.sh` (keeps all data and settings).
- **Status and logs:** `systemctl status twinstack-gate`, `journalctl -u twinstack-gate -n 100`.
- **Restore a backup:** `sudo -u postgres pg_restore --clean -d gate /var/backups/twinstack-gate/gate-YYYY-MM-DD.dump`,
  then `sudo systemctl restart twinstack-gate`.
