#!/usr/bin/env bash

PIDFILE="/tmp/minecraft_macro_loop.pid"

if [ -e "$PIDFILE" ]; then
    # 1. Stop the running background loop
    PID=$(cat "$PIDFILE")
    kill "$PID" 2>/dev/null
    rm -f "$PIDFILE"
    
    notify-send "Macro" "Loop Stopped" -t 1500 -i input-mouse
else
    # 2. Grab the unique address of the currently focused game window
    TARGET_WINDOW=$(hyprctl activewindow -j | jq -r '.address')
    
    # Failsafe: Ensure a window is actually focused
    if [ "$TARGET_WINDOW" == "null" ] || [ -z "$TARGET_WINDOW" ]; then
        notify-send "Macro Error" "No active window found to lock onto." -t 2000
        exit 1
    fi

    # 3. Start the infinite loop in the background
    (
        while true; do
            # SAFETY SWITCH: Check if your locked game window is still the active one.
            # This prevents the macro from clicking across your screen if you switch workspaces or Alt-Tab!
            CURRENT_WINDOW=$(hyprctl activewindow -j | jq -r '.address')
            
            if [ "$CURRENT_WINDOW" == "$TARGET_WINDOW" ]; then
                # --- MACRO SEQUENCE START ---
                wdotool keydown s
                wdotool keydown a
                wdotool mousemove 960 540
                sleep 0.08
                ydotool click 0xC1
                sleep 0.07
                wdotool keyup s
                wdotool keyup a

                wdotool mousemove 502 996
                wdotool keydown s
                wdotool keydown a
                sleep 0.08
                for i in {1..30}; do
                    ydotool click 0xC0
                    sleep 0.05
                done
                wdotool keyup s
                wdotool keyup a
                wdotool keydown s
                wdotool keydown a
                
                hyprctl dispatch focuswindow "address:${TARGET_WINDOW}"
                #wdotool key Escape
                sleep 0.13
                wdotool keyup s
                wdotool keyup a
                #wdotool key Escape
                hyprctl dispatch focuswindow "address:${TARGET_WINDOW}"
                # --- MACRO SEQUENCE END ---
            fi
            wdotool keydown s
            wdotool keydown a
            sleep 0.3
            for i in {1..20}; do
                    ydotool click 0xC0
                    sleep 0.05
                done
            wdotool keyup s
            wdotool keyup a
            wdotool keydown s
            wdotool keydown a
            sleep 0.3
            for i in {1..20}; do
                    ydotool click 0xC0
                    sleep 0.05
                done
            wdotool keyup s
            wdotool keyup a
            wdotool keydown s
            wdotool keydown a
            # Wait 0.4 seconds before starting the next iteration
            sleep 0.8
            wdotool keyup s
            wdotool keyup a
        done
    ) &
    
    # 4. Save the background process ID so pressing the bind again kills it
    echo $! > "$PIDFILE"
    
    # Fetch the class of the window for the notification banner
    TARGET_NAME=$(hyprctl activewindow -j | jq -r '.class')
    notify-send "Macro" "Looping locked to: $TARGET_NAME" -t 1500 -i input-mouse
fi