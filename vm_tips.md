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

This helps at every layer - on the hypervisor host running the VM, and on the VM running
the vrnetlab VMs with nested virtualisation.

It could possibly even help with the third layer of virtualisation when running the Juniper
BSD VM inside the Juniper vmhost bit... but I didn't go messing with that layer.

It definitely reduced CPU usage substantially.  Same number of active cores, less busy.


## VM definition

Edit with `virsh edit <vm>`, then do a **full shutdown and start** (a reboot from inside the guest doesn't apply these).

### 4. CPU passthrough with migration safety off

Exposes `svm` for nesting and the invariant TSC (`constant_tsc`, `nonstop_tsc`). This was the key fix for vJunos booting at normal speed.

```xml
<cpu mode='host-passthrough' check='none' migratable='off'/>
```

### 6. Memballoon & freePageReporting

This means the hypervisor host does not allocate more memory for the VM than what it is actually using, or thereabouts.

It enables us to set a high maximum memory for when we need it, without locking up that amount of RAM the entire time.

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

Normally Linux will try to use all free memory it can for page and file cache, thinking it may as well.

With the freePageReporting enabled for the VM the hypervisor only needs assign however much RAM the VM
is actually using, however if the VM decides to fill it to its max for caching it defeats the point.

We create a systemd unit to write '1' to /proc/sys/vm/drop_caches every few minutes to drop the existing
memory caches, which minimised the amount of memory wasted on cache inside the VM, and thus allows the
hypervisor to reclaim it thanks to freePageCaching.

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

### 11. Enable Kernel Samepage Merging (KSM)

This is a linux optimisation to find memory pages with idential contents and de-dupe them.

```bash
echo 'w /sys/kernel/mm/ksm/run - - - - 1' | sudo tee /etc/tmpfiles.d/ksm.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/ksm.conf     # apply now, without rebooting
cat /sys/kernel/mm/ksm/run                                  # should show 1
```

Not sure how much it helps, you can assess by running:
```
grep . /sys/kernel/mm/ksm/pages_sharing /sys/kernel/mm/ksm/pages_shared
```

Each is roughly 4KB mem saved.

### 12. Pass the invariant TSC into vJunos

Modern systems have an _invariant_ "Time Stamp Counter" that increases steadily over time, regardless of what speed the
CPU might be operating at / changing.  Older systems the TSC could be unreliable due to speed changes etc., so there is 
a CPU flag to expose to say "this system has an invariant TSC".  We need to expose this to the nested Juniper VMs so they
use it and qemu doesn't try to emulate it or some nonsense.

The vJunos containers run the Junipe VM images, which in turn run a VM inside (I assume they are linux and run the BSD vmhost same as a real Juniper)

This 3-layers deep nested virtualisation is quite horrible, but we can make it work:

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

