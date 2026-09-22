# Cisco 7945G factory-reset recovery

The bench phone is `CP-7945G`, MAC `0C2724317F2E`, extension `6738`.
On September 19, 2026, its CDP advertisement showed `79XX_default_load`
at `192.168.0.197`. Its DHCP request asked for options 66 and 150, but the
router at `192.168.0.1` supplied neither. The existing TFTP server also lacked
the firmware. These are separate requirements for recovery.

Recovery completed: the phone downloaded the signed firmware and registered
to Asterisk at **04:58:19 UTC on September 20, 2026** (September 19 locally).
The host firewall also needed a narrow SCCP TCP/2000 exception for this MAC;
that exception is now present in both runtime and permanent configuration.

## Firmware

SCCP release **9.4(2)SR3**, load **SCCP45.9-4-2SR3-1S**, is staged in
`tftp/files/`. Keep the following files together, with their original names:

- `term45.default.loads` (7945 recovery manifest)
- `term65.default.loads` (7965 recovery manifest)
- `SCCP45.9-4-2SR3-1S.loads`
- `apps45.9-4-2ES26.sbn`
- `cnu45.9-4-2ES26.sbn`
- `cvm45sccp.9-4-2ES26.sbn`
- `dsp45.9-4-2ES26.sbn`
- `jar45sccp.9-4-2ES26.sbn`

The existing per-phone config points to that SCCP load and to Asterisk at
`192.168.0.105:2000`. It uses the load name without the `.loads` suffix.

[Cisco's release documentation](https://www.cisco.com/web/software/282074289/134964/cmterm-7945_7965-sccp.9-4-2SR3-1-readme.html)
confirms this firmware supports the 7945G/7965G, supports standalone TFTP
installation, and uses signed images authenticated by the phone.

Cisco's software download portal returned HTTP 403 from this environment.
The archive was obtained from this third-party mirror:

<https://logiciels.ycharbi.fr/Cisco/IOS/TELEPHONE%20IP/Micrologiciels/7945-7965/SCCP/cmterm-7945_7965-sccp.9-4-2-1SR3-1.tar>

The mirror's archive filename has an extra `-1`; the enclosed release manifest
identifies `SCCP45.9-4-2SR3-1S` and references the five ES26 payloads above.
All eight members were inspected before extraction. No archive executables
were run. The archive SHA-256 is:

```text
6270b5cf34907d086fa0206a40b1ee794caa042b6c4e75fc37cb3bccc5679653
```

`firmware/9.4.2SR3/SHA256SUMS` records the extracted file hashes. These are
locally recorded integrity hashes, not independently published Cisco hashes.
Cisco's documented MD5 is for its different `.cop.sgn` installer and cannot
authenticate this repackaged TAR. The phone's firmware signature check is
left intact.

## Tell the recovery loader where to download

Preferred permanent setting: configure the router's **DHCP option 150** as
`192.168.0.105`. If the router cannot advertise this option, the optional
Compose `recovery-dhcp` service can supply it to this one phone.

`dnsmasq.conf` listens only on `enp114s0`, has no dynamic address pool, and
explicitly ignores all MAC addresses except `0c:27:24:31:7f:2e`. It offers
the address `192.168.0.197` already leased to this phone by the router, the
LAN netmask `255.255.0.0`, gateway/DNS `192.168.0.1`, and TFTP options 150,
66, and the BOOTP next-server field pointing at `192.168.0.105`. It is
temporary and does not restart automatically.

The helper uses `dhcp-authoritative` so it can acknowledge the bootloader's
INIT-REBOOT request for this known address even without a prior local lease.
The MAC filter remains active: an isolated-network check verified correct
DISCOVER/REQUEST replies for this MAC and no replies to either message
from an unrelated MAC. Do not remove the MAC filter or add a dynamic pool.

Before reusing this config later, verify that the address still belongs to
this phone in the router's lease list or reserve it for this MAC. Two DHCP
servers can race; this service does not suppress or impersonate the router.
If the phone repeatedly selects the router's reply, configure option 150
there or use an isolated recovery LAN with its own matching address scope.

Run from the project root:

```bash
docker compose up -d tftp pbx
docker compose --profile recovery up -d recovery-dhcp
docker compose --profile recovery logs -f recovery-dhcp tftp
```

Allow the phone's next boot loop, or power-cycle it once if it has stopped
retrying. Once it starts downloading, leave its power connected through
the downloads and automatic reboots. No additional factory reset is needed.

Watch for `sent ...term45.default.loads` followed by the `.sbn` transfers
to `192.168.0.197`, then check registration:

```bash
docker exec sccp-pbx asterisk -rx 'sccp show devices'
docker exec sccp-pbx asterisk -rx 'sccp show lines'
```

After recovery, either configure option 150 permanently on the router or
set `Alternate TFTP = Yes`, `TFTP Server 1 = 192.168.0.105` in the phone's
Network Configuration menu (unlock with `**#` if needed). Then stop the
temporary DHCP helper:

```bash
docker compose --profile recovery stop recovery-dhcp
```

If the phone rejects a load, preserve the exact screen error and TFTP log;
an older loader may require an intermediate Cisco firmware release. Do not
rename an unrelated model's firmware or edit signed load manifests.

The dnsmasq options used here are described in the
[dnsmasq manual](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html).
