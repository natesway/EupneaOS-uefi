#!/usr/bin/env python3
# This script is cloud oriented, so it is not very user-friendly.

import argparse
import os
import sys

from functions import *


# parse arguments from the cli. Only for testing/advanced use. All other parameters are handled by cli_input.py
def process_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", dest="dev_build", default=False, help="Use latest dev build. May be unstable.")
    parser.add_argument("--stable", dest="stable", default=False, help="Use chromeos stable kernel.")
    parser.add_argument("--exp", dest="exp", default=False, help="Use chromeos experimental 5.15 kernel.")
    parser.add_argument("--mainline-testing", dest="mainline_testing", default=False,
                        help="Use mainline testing kernel.")
    return parser.parse_args()


# Create, mount, partition the img and flash the mainline eupnea kernel
def prepare_image() -> str:
    print_status("Preparing image")

    try:
        bash(f"fallocate -l 10G eupneaos-uefi.img")
    except subprocess.CalledProcessError:  # try fallocate, if it fails use dd
        bash(f"dd if=/dev/zero of=eupneaos-uefi.img status=progress bs=1024 count={10 * 1000000}")
    print_status("Mounting empty image")
    img_mnt = bash("losetup -f --show eupneaos-uefi.img")
    if img_mnt == "":
        print_error("Failed to mount image")
        exit(1)

    # partition image
    print_status("Preparing device/image partition")

    # format as per depthcharge requirements,
    # READ: https://wiki.gentoo.org/wiki/Creating_bootable_media_for_depthcharge_based_devices
    bash(f"parted -s {img_mnt} mklabel gpt")
    bash(f"parted -s -a optimal {img_mnt} unit mib mkpart Kernel 1 65")  # kernel partition
    bash(f"parted -s -a optimal {img_mnt} unit mib mkpart Kernel 65 129")  # reserve kernel partition
    bash(f"parted -s -a optimal {img_mnt} unit mib mkpart Root 129 629") # EFI System Partition
    bash(f"parted -s -a optimal {img_mnt} unit mib mkpart Root 629 100%")  # rootfs partition
    bash(f"cgpt add -i 1 -t kernel -S 1 -T 5 -P 15 {img_mnt}")  # set kernel flags
    bash(f"cgpt add -i 2 -t kernel -S 1 -T 5 -P 1 {img_mnt}")  # set backup kernel flags

    print_status("Formatting rootfs part")
    rootfs_mnt = img_mnt + "p4"  # fourth partition is rootfs
    esp_mnt = img_mnt + "p3"
    # Create rootfs ext4 partition
    bash(f"yes 2>/dev/null | mkfs.ext4 {rootfs_mnt}")  # 2>/dev/null is to supress yes broken pipe warning
    # Create esp fat32 partition
    bash(f"yes 2>/dev/null | mkfs.fat -F 32 {esp_mnt}")  # 2>/dev/null is to supress yes broken pipe warning
    # Mount rootfs partition
    bash(f"mount {rootfs_mnt} /mnt/eupneaos")
    # Mount esp
    bash("mkdir -p /mnt/eupneaos/boot")
    bash(f"mount {esp_mnt} /mnt/eupneaos/boot")

    # get uuid of rootfs partition
    rootfs_partuuid = bash(f"blkid -o value -s PARTUUID {rootfs_mnt}")
    # write PARTUUID to kernel flags and save it as a file
    with open(f"configs/kernel.flags", "r") as flags:
        temp_cmdline = flags.read().replace("insert_partuuid", rootfs_partuuid).strip()
    with open("kernel.flags", "w") as config:
        config.write(temp_cmdline)

    print_status("Partitioning complete")
    flash_kernel(f"{img_mnt}p1")
    return img_mnt


def flash_kernel(kernel_part: str) -> None:
    print_status("Flashing kernel to device/image")
    # Sign kernel
    bash("futility vbutil_kernel --arch x86_64 --version 1 --keyblock /usr/share/vboot/devkeys/kernel.keyblock"
         + " --signprivate /usr/share/vboot/devkeys/kernel_data_key.vbprivk --bootloader kernel.flags" +
         " --config kernel.flags --vmlinuz /tmp/eupneaos-build/bzImage --pack /tmp/eupneaos-build/bzImage.signed")
    bash(f"dd if=/tmp/eupneaos-build/bzImage.signed of={kernel_part}")  # part 1 is the kernel partition

    print_status("Kernel flashed successfully")


# Make a bootable rootfs
def bootstrap_rootfs() -> None:
    bash("tar xfp /tmp/eupneaos-build/rootfs.tar.xz -C /mnt/eupneaos --checkpoint=.10000")
    # Create a temporary resolv.conf for internet inside the chroot
    mkdir("/mnt/eupneaos/run/systemd/resolve", create_parents=True)  # dir doesnt exist coz systemd didnt run
    cpfile("/etc/resolv.conf",
           "/mnt/eupneaos/run/systemd/resolve/stub-resolv.conf")  # copy hosts resolv.conf to chroot

    # TODO: Replace generic repos with own EupneaOS repos
    chroot("dnf install --releasever=37 --allowerasing -y generic-logos generic-release generic-release-common")
    chroot("dnf group install -y 'Hardware Support'")
    chroot("dnf group install -y 'Common NetworkManager Submodules'")
    chroot("dnf install -y linux-firmware")
    chroot("dnf install -y git vboot-utils rsync cloud-utils parted grub2-efi-x64 efibootmgr")  # postinstall dependencies
    chroot("dnf install -y grub2-efi-x64-modules grub2-efi grub2-efi-modules shim")

    # Add RPMFusion repos
    chroot(f"dnf install -y https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-37.noarch.rpm")
    chroot(f"dnf install -y https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-37.noarch.rpm")
    bash(f"mount -t proc none /mnt/eupneaos/proc && mount -o bind /dev /mnt/eupneaos/dev")



def get_uuids(img_mnt: None) -> list:
    bootpart = img_mnt + "p3"
    rootpart = img_mnt + "p4"
    bootuuid = bash(f"blkid -o value -s PARTUUID {bootpart}")
    rootuuid = bash(f"blkid -o value -s PARTUUID {rootpart}")
    uuids = [bootuuid, rootuuid]
    return uuids


def configure_rootfs(uuids) -> None:
    # Extract kernel modules
    print_status("Extracting kernel modules")
    rmdir("/mnt/eupneaos/lib/modules")  # remove all old modules
    mkdir("/mnt/eupneaos/lib/modules")
    bash(f"tar xpf /tmp/eupneaos-build/modules.tar.xz -C /mnt/eupneaos/lib/modules/ --checkpoint=.10000")
    print("")  # break line after tar

    # Enable loading modules needed for eupnea
    cpfile("configs/eupnea-modules.conf", "/mnt/eupneaos/etc/modules-load.d/eupnea-modules.conf")

    # Extract kernel headers
    print_status("Extracting kernel headers")
    dir_kernel_version = bash(f"ls /mnt/eupneaos/lib/modules/").strip()  # get modules dir name
    rmdir(f"/mnt/eupneaos/usr/src/linux-headers-{dir_kernel_version}", keep_dir=False)  # remove old headers
    mkdir(f"/mnt/eupneaos/usr/src/linux-headers-{dir_kernel_version}", create_parents=True)
    bash(f"tar xpf /tmp/eupneaos-build/headers.tar.xz -C /mnt/eupneaos/usr/src/linux-headers-{dir_kernel_version}/ "
         f"--checkpoint=.10000")
    print("")  # break line after tar
    chroot(f"ln -s /usr/src/linux-headers-{dir_kernel_version}/ "
           f"/lib/modules/{dir_kernel_version}/build")  # use chroot for correct symlink

    # copy previously downloaded firmware
    print_status("Copying google firmware")
    start_progress(force_show=True)  # start fake progress
    cpdir("linux-firmware", "/mnt/eupneaos/lib/firmware")
    stop_progress(force_show=True)  # stop fake progress

    print_status("Configuring liveuser")
    chroot("useradd --create-home --shell /bin/bash liveuser")  # add user
    chroot("usermod -aG wheel liveuser")  # add user to wheel
    chroot(f'echo "liveuser:eupneaos" | chpasswd')  # set password to eupneaos
    # set up automatic login on boot for temp-user
    with open("/mnt/eupneaos/etc/sddm.conf", "a") as sddm_conf:
        sddm_conf.write("\n[Autologin]\nUser=liveuser\nSession=plasma.desktop\n")

    print_status("Copying eupnea scripts and configs")
    # Copy postinstall scripts
    for file in Path("postinstall-scripts").iterdir():
        if file.is_file():
            if file.name == "LICENSE" or file.name == "README.md" or file.name == ".gitignore":
                continue  # dont copy license, readme and gitignore
            else:
                cpfile(file.absolute().as_posix(), f"/mnt/eupneaos/usr/local/bin/{file.name}")

    # copy audio setup script
    cpfile("audio-scripts/setup-audio", "/mnt/eupneaos/usr/local/bin/setup-audio")

    # copy functions file
    cpfile("functions.py", "/mnt/eupneaos/usr/local/bin/functions.py")
    chroot("chmod 755 /usr/local/bin/*")  # make scripts executable in system

    # copy configs
    mkdir("/mnt/eupneaos/etc/eupnea")
    cpdir("configs", "/mnt/eupneaos/etc/eupnea")  # eupnea general configs
    cpdir("postinstall-scripts/configs", "/mnt/eupneaos/etc/eupnea")  # postinstall configs
    cpdir("audio-scripts/configs", "/mnt/eupneaos/etc/eupnea")  # audio configs

    # copy preset eupnea settings file for postinstall scripts to read
    cpfile("configs/eupnea.json", "/mnt/eupneaos/etc/eupnea.json")

    # Install systemd services
    print_status("Installing systemd services")
    # Copy postinstall scripts
    for file in Path("systemd-services").iterdir():
        if file.is_file():
            if file.name == "LICENSE" or file.name == "README.md" or file.name == ".gitignore":
                continue  # dont copy license, readme and gitignore
            else:
                cpfile(file.absolute().as_posix(), f"/mnt/eupneaos/etc/systemd/system/{file.name}")
    chroot("systemctl enable eupnea-postinstall.service")
    chroot("systemctl enable eupnea-update.timer")

    print_status("Fixing sleep")
    # disable hibernation aka S4 sleep, READ: https://eupnea-linux.github.io/main.html#/pages/bootlock
    # TODO: Fix S4 sleep
    mkdir("/mnt/eupneaos/etc/systemd/")  # just in case systemd path doesn't exist
    with open("/mnt/eupneaos/etc/systemd/sleep.conf", "a") as conf:
        conf.write("SuspendState=freeze\nHibernateState=freeze\n")

    # systemd-resolved.service needed to create /etc/resolv.conf link. Not enabled by default for some reason
    chroot("systemctl enable systemd-resolved")

    # Fix fstab issue?
    bash("touch /mnt/eupneaos/etc/fstab")
    # Append lines to fstab
    with open("/mnt/eupneaos/etc/fstab", "r") as fstab:
        oldfstab = fstab
    with open("/mnt/eupneaos/etc/fstab", "w") as fstab:
        fstab = f"\nUUID={uuids[0]} /boot vfat rw,relatime,fmask=0022,dmask=0022,codepage=437 0 2\n{uuids[1]} / ext4 rw,relatime 0 1"

    # Install grub
    chroot("grub2-mkconfig -o /boot/grub/grub.cfg")
    chroot("grub2-mkconfig -o /boot/grub2/grub.cfg")
    chroot("grub2-install --target=x86_64-efi --efi-directory=/boot --removable")
    chroot("grub2-mkconfig -o /boot/grub/grub.cfg")


def customize_kde() -> None:
    # Install KDE
    chroot("dnf group install -y 'KDE Plasma Workspaces'")
    # Set system to boot to gui
    chroot("systemctl set-default graphical.target")

    # Set kde ui settings
    print_status("Setting General UI settings")
    mkdir("/mnt/eupneaos/home/liveuser/.config")
    cpfile("configs/kde-configs/kwinrc", "/mnt/eupneaos/home/liveuser/.config/kwinrc")  # set general kwin settings
    cpfile("configs/kde-configs/kcminputrc", "/mnt/eupneaos/home/liveuser/.config/kcminputrc")  # set touchpad settings
    chroot("chown -R liveuser:liveuser /home/liveuser/.config")  # set permissions

    print_status("Installing global kde theme")
    # Installer needs to be run from within chroot
    cpdir("eupneaos-theme", "/mnt/eupneaos/tmp/eupneaos-theme")
    # run installer script from chroot
    chroot("cd /tmp/eupneaos-theme && bash /tmp/eupneaos-theme/install.sh")  # install global theme

    # apply global dark theme


def relabel_files() -> None:
    # Fedora requires all files to be relabled for SELinux to work
    # If this is not done, SELinux will prevent users from logging in
    print_status("Relabeling files for SELinux")

    # copy /proc files needed for fixfiles
    mkdir("/mnt/eupneaos/proc/self")
    cpfile("configs/selinux/mounts", "/mnt/eupneaos/proc/self/mounts")
    cpfile("configs/selinux/mountinfo", "/mnt/eupneaos/proc/self/mountinfo")

    # copy /sys files needed for fixfiles
    mkdir("/mnt/eupneaos/sys/fs/selinux/initial_contexts/", create_parents=True)
    cpfile("configs/selinux/unlabeled", "/mnt/eupneaos/sys/fs/selinux/initial_contexts/unlabeled")

    # Backup original selinux
    cpfile("/mnt/eupneaos/usr/sbin/fixfiles", "/mnt/eupneaos/usr/sbin/fixfiles.bak")
    # Copy patched fixfiles script
    cpfile("configs/selinux/fixfiles", "/mnt/eupneaos/usr/sbin/fixfiles")

    chroot("/sbin/fixfiles -T 0 restore")

    # Restore original fixfiles
    cpfile("/mnt/eupneaos/usr/sbin/fixfiles.bak", "/mnt/eupneaos/usr/sbin/fixfiles")
    rmfile("/mnt/eupneaos/usr/sbin/fixfiles.bak")


# Shrink image to actual size
def compress_image(img_mnt: str) -> None:
    print_status("Shrinking image")
    bash(f"e2fsck -fpv {img_mnt}p3")  # Force check filesystem for errors
    bash(f"resize2fs -f -M {img_mnt}p3")
    block_count = int(bash(f"dumpe2fs -h {img_mnt}p3 | grep 'Block count:'")[12:].split()[0])
    actual_fs_in_bytes = block_count * 4096
    # the kernel part is always the same size -> sector amount: 131072 * 512 => 67108864 bytes
    # There are 2 kernel partitions -> 67108864 bytes * 2 = 134217728 bytes
    actual_fs_in_bytes += 134217728
    actual_fs_in_bytes += 20971520  # add 20mb for linux to be able to boot properly
    bash(f"truncate --size={actual_fs_in_bytes} ./eupneaos-uefi.img")

    # compress image to tar. Tars are smaller but the native file manager on chromeos cant uncompress them
    # These are stored as backups in the GitHub releases
    bash("tar -cv -I 'xz -9 -T0' -f ./eupneaos-uefi.img.tar.xz ./eupneaos-uefi.img")

    # Rar archives are bigger, but natively supported by the ChromeOS file manager
    # These are uploaded as artifacts and then manually uploaded to a cloud storage
    bash("rar a eupneaos-uefi.img.rar -m5 eupneaos-uefi.img")

    print_status("Calculating sha256sums")
    # Calculate sha256sum sums
    with open("eupneaos-uefi.sha256", "w") as file:
        file.write(bash("sha256sum eupneaos-uefi.img eupneaos-uefi.img.tar.xz "
                        "eupneaos-uefi.img.rar"))


def chroot(command: str) -> None:
    bash(f'chroot /mnt/eupneaos /bin/bash -c "{command}"')  # always print output


if __name__ == "__main__":
    args = process_args()  # process args
    set_verbose(True)  # increase verbosity

    # parse arguments
    kernel_type = "mainline"
    if args.dev_build:
        print_warning("Using dev release")
    if args.exp:
        print_warning("Using experimental chromeos kernel")
        kernel_type = "exp"
    if args.stable:
        print_warning("Using stable chromes kernel")
        kernel_type = "stable"
    if args.mainline_testing:
        print_warning("Using mainline testing kernel")
        kernel_type = "mainline-testing"

    # prepare mount
    mkdir("/mnt/eupneaos", create_parents=True)

    image_props = prepare_image()
    uuids = get_uuids(image_props)
    bootstrap_rootfs()
    configure_rootfs(uuids)
    customize_kde()
    relabel_files()

    # Clean image of temporary files
    rmdir("/mnt/eupneaos/tmp")
    rmdir("/mnt/eupneaos/var/tmp")
    rmdir("/mnt/eupneaos/var/cache")
    rmdir("/mnt/eupneaos/proc")
    rmdir("/mnt/eupneaos/run")
    rmdir("/mnt/eupneaos/sys")
    rmdir("/mnt/eupneaos/lost+found")
    rmdir("/mnt/eupneaos/dev")
    rmfile("/mnt/eupneaos/.stop_progress")

    # Force unmount image
    bash("umount -f /mnt/eupneaos")
    sleep(5)  # wait for umount to finish
    compress_image(image_props)

    bash(f"losetup -d {image_props}")  # unmount image

    print_header("Image creation completed successfully!")
