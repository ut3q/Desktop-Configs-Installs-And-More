#!/bin/bash

# ==========================================
# 1. CONFIGURATION
# ==========================================
# Delay between clicks in seconds. 
# 0.2 = 5 clicks per second. 0.1 = 10 clicks per second.
CLICK_DELAY=0.02
# ==========================================

PIDFILE="/tmp/autoclicker.pid"

if [ -e "$PIDFILE" ]; then
    # Stop the running process
    PID=$(cat "$PIDFILE")
    kill "$PID" 2>/dev/null
    rm -f "$PIDFILE"
    
    notify-send "Autoclicker" "Stopped"
else
    # 1. Grab the unique address of the currently focused window
    TARGET_WINDOW=$(hyprctl activewindow -j | jq -r '.address')
    
    # 2. Failsafe: Ensure a window is actually focused
    if [ "$TARGET_WINDOW" == "null" ] || [ -z "$TARGET_WINDOW" ]; then
        notify-send "Autoclicker Error" "No active window found to lock onto."
        exit 1
    fi

    # 3. Start the autoclicker loop in the background
    (
        while true; do
            # Check if the locked window is still the active one
            CURRENT_WINDOW=$(hyprctl activewindow -j | jq -r '.address')
            
            if [ "$CURRENT_WINDOW" == "$TARGET_WINDOW" ]; then
                # 0xC0 represents a full left-click (down + up)
                #wdotool click 3
                #ydotool click 0xC0
                ydotool click 0xC1
            fi
            
            # Wait for the configured delay before the next loop
            sleep "$CLICK_DELAY"
        done
    ) &
    
    # Save the background process ID
    echo $! > "$PIDFILE"
    
    # Fetch the class of the window for the notification
    TARGET_NAME=$(hyprctl activewindow -j | jq -r '.class')
    notify-send "Autoclicker" "Locked to: $TARGET_NAME"
fi