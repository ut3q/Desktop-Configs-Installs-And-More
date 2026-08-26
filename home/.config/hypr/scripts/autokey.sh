#!/bin/bash

# ==========================================
# 1. CONFIGURATION
# ==========================================
# Syntax for "click": ["key"]="click <interval>"
# Syntax for "hold":  ["key"]="hold <hold_time> <wait_time>"
# Times are in seconds (decimals like 0.5 work fine!)

declare -A KEY_CONFIG=(
    #["f"]="hold 60 0.3"    # Click 'f' every 0.5 seconds
    #["c"]="hold 60 10" # Hold 'c' for 3s, release, wait 1s
    ["f"]="hold 0.08 0.05"      # Click 'v' every 2 seconds
)
# ==========================================

PIDFILE="/tmp/autokey_pids.txt"

declare -A KEYMAP=(
    ["a"]=30 ["b"]=48 ["c"]=46 ["d"]=32 ["e"]=18 ["f"]=33 ["g"]=34 
    ["h"]=35 ["i"]=23 ["j"]=36 ["k"]=37 ["l"]=38 ["m"]=50 ["n"]=49 
    ["o"]=24 ["p"]=25 ["q"]=16 ["r"]=19 ["s"]=31 ["t"]=20 ["u"]=22 
    ["v"]=47 ["w"]=17 ["x"]=45 ["y"]=21 ["z"]=44
    ["1"]=2  ["2"]=3  ["3"]=4  ["4"]=5  ["5"]=6  
    ["6"]=7  ["7"]=8  ["8"]=9  ["9"]=10 ["0"]=11
    ["space"]=57 ["enter"]=28 ["shift"]=42 ["ctrl"]=29 ["alt"]=56
)

if [ -e "$PIDFILE" ]; then
    # -- STOPPING SEQUENCE --
    while read -r pid; do
        kill "$pid" 2>/dev/null
    done < "$PIDFILE"
    
    rm -f "$PIDFILE"

    # Force release keys
    RELEASE_CMDS=""
    for key in "${!KEY_CONFIG[@]}"; do
        code=${KEYMAP[$key]}
        if [ -n "$code" ]; then
            RELEASE_CMDS+="$code:0 "
        fi
    done
    
    if [ -n "$RELEASE_CMDS" ]; then
        ydotool key $RELEASE_CMDS
    fi

    notify-send "AutoKey" "Stopped all custom loops"
else
    # -- STARTING SEQUENCE --
    > "$PIDFILE" 

    TARGET_WINDOW=$(hyprctl activewindow -j | jq -r '.address')
    
    if [ "$TARGET_WINDOW" == "null" ] || [ -z "$TARGET_WINDOW" ]; then
        notify-send "AutoKey Error" "No active window found to lock onto."
        exit 1
    fi

    # 3. Start the loops
    for key in "${!KEY_CONFIG[@]}"; do
        code="${KEYMAP[$key]}"
        
        # Split the configuration string into variables
        read -r mode param1 param2 <<< "${KEY_CONFIG[$key]}"

        if [ -z "$code" ]; then
            notify-send "AutoKey Error" "Key '$key' is not in the dictionary."
            continue
        fi

        if [ "$mode" == "click" ]; then
            # Default to 1s interval if no parameter was provided
            interval=${param1:-1} 
            
            (
                while true; do
                    CURRENT_WINDOW=$(hyprctl activewindow -j | jq -r '.address')
                    if [ "$CURRENT_WINDOW" == "$TARGET_WINDOW" ]; then
                        ydotool key "$code:1" "$code:0"
                    fi
                    sleep "$interval"
                done
            ) &
            echo $! >> "$PIDFILE"
            
        elif [ "$mode" == "hold" ]; then
            # Default to 5s hold and 1s wait if no parameters were provided
            hold_time=${param1:-5}
            wait_time=${param2:-1}
            
            (
                while true; do
                    CURRENT_WINDOW=$(hyprctl activewindow -j | jq -r '.address')
                    if [ "$CURRENT_WINDOW" == "$TARGET_WINDOW" ]; then
                        ydotool key "$code:1"
                        sleep "$hold_time"
                        ydotool key "$code:0"
                        sleep "$wait_time"
                    else
                        # If tabbed away, check again in 1 second
                        sleep 1 
                    fi
                done
            ) &
            echo $! >> "$PIDFILE"
            
        else
            notify-send "AutoKey Error" "Unknown mode '$mode' for key '$key'"
        fi
    done

    TARGET_NAME=$(hyprctl activewindow -j | jq -r '.class')
    notify-send "AutoKey" "Custom timing locked to: $TARGET_NAME"
fi