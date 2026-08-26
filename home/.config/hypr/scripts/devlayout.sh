#!/bin/bash

check_and_launch() {
    local class_name=$1
    local launch_cmd=$2
    
    # Check if the class is already running
    if ! hyprctl clients -j | jq -e ".[] | select(.class == \"$class_name\")" > /dev/null; then
        $launch_cmd &
    fi
}

# 1. Zen Browser (Class was 'zen')
check_and_launch "Helium" "/home/aqua/Applications/Helium.AppImage" # "zen" "zen-browser"

# 2. VSCodium (Flatpak + Wayland Flags)
check_and_launch "com.vscodium.codium" "codium --enable-features=UseOzonePlatform --ozone-platform=wayland"

# 3. Vinegar (Class was 'explorer.exe')
check_and_launch "explorer.exe" "flatpak run org.vinegarhq.Vinegar"