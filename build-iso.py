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
    chroot("dnf install -y kernel")

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
    # Enable loading modules needed for eupnea
    cpfile("configs/eupnea-modules.conf", "/mnt/eupneaos/etc/modules-load.d/eupnea-modules.conf")

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

    bootstrap_rootfs()
    configure_rootfs(uuids)
    customize_kde()
    relabel_files()

    # Force unmount image
    bash("umount -f /mnt/eupneaos")
    sleep(5)  # wait for umount to finish
    compress_image(image_props)

    print_header("Image creation completed successfully!")

