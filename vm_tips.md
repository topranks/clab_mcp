# Nested containerlab VM: setup and optimisations

Everything needed, in order, to run containerlab (including vrnetlab-based vJunos) inside a libvirt/KVM VM on an AMD host, as currently working.

## Host

### 1. Nested virtualisation enabled

Usually on by default. Check:

```bash
cat /sys/module/kvm_amd/parameters/nested    # should be 1
```

If not:

```bash
echo "options kvm_amd nested=1" | sudo tee /etc/modprobe.d/kvm-nested.conf
# reboot (or reload kvm_amd with all VMs shut down)
```

### 2. Halt polling off for the VM's vCPUs (recommended)

```bash
echo 'options kvm halt_poll_ns=0' | sudo tee /etc/modprobe.d/kvm-halt-poll.conf
echo 0 | sudo tee /sys/module/kvm/parameters/halt_poll_ns    # apply now
```

### 3. Firewall rules for the VM

With a default-drop nftables input policy, allow DHCP and DNS from `virbr0` (note `udp dport`, not `tcp dport`), plus service ports used by the VM (11434 for Ollama, 8086 for the proxy):

```
set VIRBR_PORTS {
        type inet_service
        flags interval
        elements = { 8086, 11434, 53 }
}

set VIRBR_UDP {
        type inet_service
        flags interval
        elements = { 53, 67 }
}

iifname "virbr0" tcp dport @VIRBR_PORTS accept
iifname "virbr0" udp dport @VIRBR_UDP accept
```

Pin the VM's address with a DHCP reservation so per-IP rules stay valid:

```bash
virsh domiflist <vm>     # get the VM's MAC
virsh net-update default add ip-dhcp-host \
  "<host mac='52:54:00:xx:xx:xx' ip='192.168.122.x'/>" --live --config
```

## VM definition

Edit with `virsh edit <vm>`, then do a **full shutdown and start** (a reboot from inside the guest doesn't apply these).

### 4. CPU passthrough with migration safety off

Exposes `svm` for nesting and the invariant TSC (`constant_tsc`, `nonstop_tsc`). This was the key fix for vJunos booting at normal speed.

```xml
<cpu mode='host-passthrough' check='none' migratable='off'/>
```

### 5. vCPUs below the host's thread count

14 vCPUs on a 16-thread host, leaving room for the host itself.

### 6. Memory that returns to the host

Maximum and current memory set to the same value, with free page reporting on the balloon device (inside `<devices>`; modify the existing `<memballoon>` entry if there is one, keeping its `<address>` line):

```xml
<memory unit='GiB'>24</memory>
<currentMemory unit='GiB'>24</currentMemory>
...
<memballoon model='virtio' freePageReporting='on'>
  <stats period='10'/>
</memballoon>
```

Make sure there's no `<memoryBacking>` with `<locked/>`, `<hugepages/>` or `<allocation mode='immediate'/>`, since those pin guest memory on the host.

Check actual host usage with `virsh dommemstat <vm>` (the `rss` line).

### 7. Disk caching (recommended)

Stops the host caching the VM's disk on top of the guest's own cache. On the disk's `<driver>` line (keep `type` as it is):

```xml
<driver name='qemu' type='qcow2' cache='none' io='native' discard='unmap'/>
```

## Inside the VM

### 8. Check KVM is usable

```bash
ls -l /dev/kvm
lsmod | grep kvm_amd
grep -m1 flags /proc/cpuinfo | tr ' ' '\n' | grep -xE 'svm|npt|constant_tsc|nonstop_tsc'
```

All four flags should be listed.

### 9. Halt polling off

The biggest CPU saving: busy cores dropped from ~100% to ~35%.

```bash
echo 'options kvm halt_poll_ns=0' | sudo tee /etc/modprobe.d/kvm-halt-poll.conf
echo 0 | sudo tee /sys/module/kvm/parameters/halt_poll_ns
```

### 10. Periodic page cache reclaim

Lets freed memory flow back to the host via free page reporting.

`/usr/local/sbin/reclaim-cache` (mode 755):

```bash
#!/bin/sh
# Only reclaim if the page cache has grown past ~2 GB
cached_kb=$(awk '/^Cached:/ {print $2}' /proc/meminfo)
[ "$cached_kb" -lt 2097152 ] && exit 0

sync
echo 1 > /proc/sys/vm/drop_caches
# Full compaction only when no lab is running (it can briefly stall processes)
if [ -z "$(docker ps -q 2>/dev/null)" ]; then
    echo 1 > /proc/sys/vm/compact_memory
fi
```

`/etc/systemd/system/reclaim-cache.service`:

```ini
[Unit]
Description=Drop page cache so free pages return to the host

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/reclaim-cache
```

`/etc/systemd/system/reclaim-cache.timer`:

```ini
[Unit]
Description=Periodic page cache reclaim

[Timer]
OnBootSec=15min
OnUnitActiveSec=3min
AccuracySec=10s

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo chmod 755 /usr/local/sbin/reclaim-cache
sudo systemctl daemon-reload
sudo systemctl enable --now reclaim-cache.timer
systemctl list-timers reclaim-cache.timer
```

### 11. KSM (optional)

Saves memory when running several identical vJunos nodes:

```bash
echo 1 | sudo tee /sys/kernel/mm/ksm/run
```

## Containerlab and vJunos

### 12. Pass the invariant TSC into vJunos

QEMU's `-cpu host` also strips the invariant TSC by default, so the vJunos Linux layer and its PFE lose their reliable clock. Set it once per kind, inside `topology:`:

```yaml
topology:
  kinds:
    juniper_vjunosrouter:
      image: vrnetlab/juniper_vjunos-router:25.4R1-S2.3
      env:
        QEMU_CPU: host,+invtsc
```

Confirm with:

```bash
ps -eo args | grep '[q]emu-system' | grep -oE -- '-cpu [^ ]+'    # -cpu host,+invtsc
```

Do the same for any other vrnetlab kinds in use, e.g. `juniper_vjunosswitch`.

### 13. Leave vJunos at 4 vCPUs and ~5 GB RAM

Don't lower `QEMU_SMP`: the PFE's DPDK engine needs its cores.

### 14. At least one data link per vJunos node

Without a data interface the FPC stays at `Present` forever. This includes single-node tests (`--node-filter` also drops links to filtered-out nodes). Verify with:

```
show chassis fpc            # FPC 0 Online
show interfaces terse ge-0/0/*
```

### 15. Build vJunos images with current vrnetlab

April 2026 or later: the build patches vJunos's Intel-only nested-virtualisation check (`vmx`) so it works on AMD (`svm`).

### 16. Habits

- Don't `docker restart` lab nodes: they lose their links. Recreate with containerlab instead.
- Destroy with `--cleanup` when a startup config needs reapplying (containerlab doesn't overwrite an existing node config directory).
- Add `clab-*/` to each lab repo's `.gitignore`.
- vJunos CLI is reached by SSH to the management IP (`admin` / `admin@123` by default); the port 5000 console is the vJunos Linux layer, not Junos.

## Not done, possible later

Setting `halt_poll_ns=0` inside the vJunos image's own Linux layer (which runs KVM for the Junos control plane), by modifying the qcow2, if CPU becomes the limit with larger Juniper labs.
