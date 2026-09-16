# Arlo E3 Wired – Full CLI Command Reference

> **CLI format:** `cli <command-group> <command> <args ...>`  
> **Sources:** [Tips and Useful Commands](https://arlo.atlassian.net/wiki/spaces/AFS/pages/63449429) · [How to use CLI on Console](https://arlo.atlassian.net/wiki/spaces/AFS/pages/63449494)

---

## Quick Tips / Useful Commands

### Update URL

```bash
# Get UpdateURL
arlocmd update_url
# or via sqlite
sqlite3 /etc/arlo/arlogw.db 'select value from kvstore where key = "KV_BS_UPDATE_URL"'

# Set UpdateURL
arlocmd update_url <url>
# Examples:
arlocmd update_url https://arloupdates.arlo.com/arlo/fw/fw_deployed/dev
arlocmd update_url https://arloupdates.arlo.com/arlo/fw/fw_deployed/dev/corrupt
arlocmd update_url http://127.0.0.1:8080
```

### Update Refresh

```bash
arlocmd update_refresh       # force=0
arlocmd update_refresh 1     # force=1
```

### Manual Firmware Update

```bash
fwupgrade <URL>
# or
arloutil manual_upgrade [options] <model_id> <version> <url>
# Example:
arloutil manual_upgrade SH1001 0.3.66_34cdd7a https://updates.arlo.com/arlo/fw/fw_deployed/dev/binaries/SH1001/SH1001-0.3.66_34cdd7a.enc
```

### Factory Reset

```bash
arlocmd factory_reset
```

### Check Env

```bash
info
# or
fw_printenv stage
```

### Migrate

```bash
arlocmd migrate <stage>   # dev | qa | prod | ftrial
# Example:
arlocmd migrate qa
```

### Device Info

```bash
arlocmd device_info
```

### Get Serial Number

```bash
cli caliget sn
```

### Set Log Level

```bash
arloutil log_level debug,battery=trace
asgard_cmd set-log-level -f debug
```

### Run vzcmd from arloutil

```bash
arlocmd json '{"resource":"basestation","action":"refreshUpdateRules","properties":{"performUpdate":false}}'
# Equivalent vzcmd:
vzcmd --expandJson PassThru='{"resource":"basestation","action":"refreshUpdateRules","properties":{"performUpdate":false}}'
```

### Logs

```bash
archive_logs                  # Tar logs to file
journalctl -a                 # View all logs
journalctl -b                 # Logs from last reboot
journalctl -f                 # Tail logs
journalctl -a -u arlod        # Logs for arlod service
journalctl -u xagent          # Logs for xagent
```

### Stop Service

```bash
systemctl stop arlod
```

### Check Version

```bash
arlod -V
cat /etc/os-release
journalctl -b -auarlod | grep version
```

### Various Utilities

```bash
arloutil    # Run without args to see all applets
```

### Automation Info/Test

```bash
arloutil arlocmd '{ "resource":"automation/test/info", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/activeMode", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/modes", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/rules", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/automations", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/schedules", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/geofences", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/shortcuts", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/panicSettings", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/panicState", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/entryExitDelay", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/exceptionSensors", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/settings", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test/state", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test", "action":"get" }'
arloutil arlocmd '{ "resource":"automation/test", "action":"sync" }'
arloutil arlocmd '{ "resource":"automation/test", "action":"arm" }'
arloutil arlocmd '{ "resource":"automation/test", "action":"home" }'
arloutil arlocmd '{ "resource":"automation/test", "action":"standby" }'
arloutil arlocmd '{ "resource":"automation/test", "action":"panic" }'
```

### WiFi Packet Capture

```bash
tcpdump -i wlan0 -w /data/maple-hub.pcap
```

### Performing OTA

```bash
# On local machine (same WiFi as camera):
python3 -m http.server 8000 --directory . --bind <ipv4addr>

# On camera:
cli ota http http://<ipv4addr>:8000/ota-ds.img
```

### Pushing arlod Binary to Device

```bash
# Build
./build.sh dolphin.arlod

# On host:
adb shell auth
arlo          # password
adb push arlod /userdata

# On device:
killall arlod
cd userdata/
chmod u+x arlod
./arlod&
```

---

## arlocmd Command Group

| Command | Arguments | Description |
|--------|-----------|-------------|
| `arlocmd bs_info` | — | Shows basestation information |
| `arlocmd log <level>` | trace \| debug \| info \| notice \| warn \| error \| crit \| alert \| emerg | Set log level |
| `arlocmd factory_reset` | — | Reset device to factory settings |
| `arlocmd migrate <stage>` | dev \| qa \| prod \| ftrial | Change platform stage and update URL |
| `arlocmd update_url [url]` | Optional URL | Get or set the update URL |
| `arlocmd update_refresh [force] [progress]` | 0 or 1 for each | Check/perform FOTA update |
| `arlocmd manual_upgrade <version> [progress]` | version string, 0 or 1 | Trigger manual firmware upgrade |
| `arlocmd dump_threads` | — | Show active arlod threads |
| `arlocmd reboot` | — | Reboot the device |

---

## Tonly Command Groups

| Command Group | Commands | Processed By |
|--------------|----------|-------------|
| `sv` | `sv save frame` — save smart vision frame [enable/disable] | sv |
| `ptz` *(PTZ models only)* | `ptz open/close [prio]`, `ptz move abs/rel/cont [prio] [h] [v] [speed]`, `ptz stop/status/reset`, `ptz get version`, `ptz track enable/disable/status`, `ptz set/get direction` | Devifd |
| `mfg build_info` | Get build info (firmware version, hashes, serial, HW version, etc.) | — |
| `mfg factory_reset` | Factory reset: 0=NO_REBOOT, 1=REBOOT, 2=RESYNC, 3=SOFTSYNC | — |
| `sntp` | Sync date/time | CLI utility |
| `ota` | `ota erase/read [partition]`, `ota bootstrap [get/set]`, `ota http [url]`, `ota stop`, `ota aging start/stop` | CLI utility |
| `caliget` / `caliset` | Get/set MFG fields: sn, mac, wifi_country_code, model_num, hw_rev, dsc4, partner_id | Devifd |
| `log set level` / `log get level` / `arlod log set/get level` | fatal, error, warn, info, debug, trace | Devifd |
| `arlod oos` | `oos status`, `oos set high`, `oos set low` — simulate out-of-service temp | — |
| `arlod battery` | `get percent/tech/charger tech/charging state/critical/temperature/thermal shutdown max\|min temp` | — |
| `arlod simulation` | `simulation motion [period] [count]`, `simulation stop motion`, `simulation ble_onboarding_start`, `simulation dnc start/stop` | — |
| `arlod aging` | `aging start/stop`, `aging ICR start/stop`, `aging speaker start/stop`, `aging ptz h/v/start/stop`, etc. | — |
| `arlod arlohandler` | `fw upgrade status`, `day night mode/status`, `sync button event`, `motion alert`, `video motion alert`, `audio alert`, `privacy shield event`, `filesystem ready` | — |
| `arlod general` | `set/get claimed status`, `get devif version`, `device firmware upgrade`, `request status [time]`, `get ssid/ip/ap mac/bssid/gateway ip/wifi_country_code`, `factory reset [no_reboot/reboot/resync]`, `perform reboot`, `force archive`, `enable debug mode [uart/usb/ssh/all/none]`, `start/stop streaming watchdog`, `flash open/write/close`, `get partner id` | — |
| `arlod camera` | `set motion zones`, `set snapshot resolution`, `set sei enabled`, `set privacy zones`, `set always on gop`, `set foresight`, `set alert backoff time`, `set anti flicker rate`, `set avc/hevc resolution/bitrate`, `set exposure compensation`, `set video mirror/flip/window`, `set max/min/target/max/min/cbr/vbr bitrate`, `set day/night framerate`, `set dusk to dawn threshold`, `get amblight mode/status`, `set ir cut filter`, `set ir led`, `set spotlight`, `get last ls value`, `get ir led on time`, `get ir cut filter` | — |
| `arlod misc` | `misc set ir cur filter [0/1]`, `misc set ir led [0/1]` | — |
| `arlod audio` | `start/stop record`, `speaker enable`, `set/get speaker volume`, `mic enable`, `set/get mic volume`, `play/stop [file]`, `set trigger action/armed/sensitivity`, `set audio recording status`, `ain drc/aec/anc/ns [on/off]`, `aout drc/vqe [on/off]`, `set/get mic gain`, `set/get speaker gain`, `reload env` | — |
| `arlod otp` | `otp key isWritten`, `otp gen key`, `otp encrypt/decrypt [len] [data]` | Devifd |
| `arlod vmd` | `vmd enable/disable`, `vmd sensitivity [1-100]`, `vmd param [threshSad] [threshMove] [switchSad] [squarePct]`, `vmd show [enable/disable]` | Devifd |
| `arlod motor` | `motor goto [h:0-348] [v:0-180] [speed:10-100]`, `motor go [h/v] [+/-delta] [speed]`, `motor reset/status`, `motor set/get mode`, `motor set max pps`, `motor set warmup steps`, `motor get version` | Devifd |
| `arlod od` | `od enable/disable` | Devifd |
| `arlod watchdog` | `watchdog start [timeout]`, `watchdog stop/enable/feed` | Devifd |
| `arlod media` | `media init/deinit`, `media stop rtsp`, `set snapshot resolution`, `request snapshot`, `set anti flicker rate/exposure/video window/privacy zones/daynight mode`, `open/read/close video stream`, `force idr frame`, `open/read audio input/output`, `write audio stream`, `set audio stream input status`, `set/get max/min/target/max bitrate`, `set/get framerate`, `get hevc vps string`, `add sei int/string`, `enable/disable sei`, `configure encoders`, `auto test start/stop`, `sensor standby/wakeup`, `mic set agc/volume/wns`, `speaker set volume`, `set siren state` | Devifd |
| `arlod led` | `led set [red/blue/green] [on/off]`, `led flash [red/blue/green] [on/off]`, `led show multicolor` | — |
| `arlod temp` | `temp start read [interval ms]`, `temp stop read`, `temp read` | — |
| `arlod sbuadc` | `sbuadc start read [interval ms]`, `sbuadc stop read`, `sbuadc read` | — |

---

## NIM (nimif) Commands

| Command | Description |
|--------|-------------|
| `nimif wifi subscribe/unsubscribe` | Subscribe/unsubscribe wifi status |
| `nimif wifi connect <ssid> [psk] [timeout]` | Connect to WiFi AP |
| `nimif wifi disconnect` | Disconnect from WiFi AP |
| `nimif wifi set wpsieparam [...]` | Set WPS parameters |
| `nimif wifi wps connect [timeout]` | Start WPS connect |
| `nimif wifi scan [ssid1] [ssid2]` | Start WiFi scan |
| `nimif wifi scan stop` | Stop WiFi scan |
| `nimif wifi set countrycode XX` | Set WiFi country code (US, CA, AU, DE, JP, SG) |
| `nimif wifi set maxmissedbeacontime <ms>` | Set max missed beacon time |
| `nimif wifi reset ber` | Reset WiFi BER stats |
| `nimif wifi reset disconnects` | Reset WiFi disconnect count |
| `nimif wifi get bssinfo` | Get WiFi BSS info |
| `nimif wifi get bssstats` | Get WiFi BSS stats |
| `nimif wifi get stats` | Get WiFi stats |
| `nimif get pluginstatus <ifname [wifi\|eth]>` | Get plug-in status |
| `nimif plugin subscribe/unsubscribe` | Subscribe/unsubscribe plugin status |
| `nimif enable/disable <ifname [eth\|wifi]>` | Enable/disable network interface |
| `nimif get/set active [ifname]` | Get/set active network interface |
| `nimif get name <ifname>` | Get interface name |
| `nimif get config <ifname>` | Get DHCP config of interface |
| `nimif dhcp subscribe/unsubscribe` | Subscribe/unsubscribe DHCP status |
| `nimif dhcp start <ifname> [timeout]` | Start DHCP via interface |
| `nimif dhcp stop <ifname>` | Stop DHCP on interface |
| `nimif time sync [ntp server] [timeout]` | Sync time (default NTP: time.google.com) |
| `nimif get mac <ifname>` | Get MAC address of interface |
| `nimif get gatewaymac` | Get gateway MAC address |
| `nimif get dhcpipmac <ifname>` | Get DHCP IP & MAC of interface |
| `nimif subscribe/unsubscribe all` | Subscribe/unsubscribe all statuses |
| `nimif eth linkstatus` | Get Ethernet link status |
| `nimif eth subscribe/unsubscribe` | Subscribe/unsubscribe Ethernet link status |
