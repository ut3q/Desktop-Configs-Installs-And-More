local vars = require("variables")
local fn = require("hyprland.functions")

hl.on("hyprland.start", function()
	-- Keyring and auth
	hl.exec_cmd("gnome-keyring-daemon --start --components=secrets")
	hl.exec_cmd("/usr/lib/polkit-gnome/polkit-gnome-authentication-agent-1")

	-- Keep clipboard contents after the source app closes (no history stored)
	hl.exec_cmd("wl-clip-persist --clipboard regular")

	-- Auto delete trash 30 days old
	hl.exec_cmd("trash-empty 30")

	-- Cursors
	hl.exec_cmd("hyprctl setcursor " .. vars.cursorTheme .. " " .. vars.cursorSize)
	hl.exec_cmd("gsettings set org.gnome.desktop.interface cursor-theme " .. vars.cursorTheme)
	hl.exec_cmd("gsettings set org.gnome.desktop.interface cursor-size " .. vars.cursorSize)

	-- Location provider and night light
	hl.exec_cmd("/usr/lib/geoclue-2.0/demos/agent")
	hl.exec_cmd("sleep 1 && gammastep")
	-- h1.exec_cmd("otd-daemon")

	-- Forward bluetooth media commands to MPRIS
	hl.exec_cmd("mpris-proxy")

	-- Resize and move windows based on matches (e.g. pip)
	hl.exec_cmd("caelestia resizer -d")
	-- --gapplication-service is deprecated in EasyEffects 8.x (the Qt rewrite);
	-- --service-mode is the current flag. Launched from Hyprland rather than a
	-- systemd --user unit on purpose: under systemd it is SIGKILLed on startup
	-- every time, while a Hyprland-spawned child runs fine. Not yet understood.
	hl.exec_cmd("easyeffects --service-mode")
	-- Start shell
	hl.exec_cmd("caelestia shell -d")
	hl.exec_cmd("python /home/aqua/Projects/ClipboardFixer.py")

	hl.exec_cmd("fish -c 'while true; sleep 86400; caelestia shell notifs clear; end'")
end)

-- Keep any Sunshine headless output pinned wherever it gets (re)created.
-- Nothing creates one at login any more -- remote desktop is on-demand, and
-- Sunshine's own ExecStartPre runs `sunshine-vdisplay init`. This is the guard
-- for when one DOES appear: Hyprland builds headless outputs from its own
-- defaults, and because they report a 0x0 physical size the auto-DPI heuristic
-- settles on scale 2, placing them at 1920x0, directly beside DP-3. `hyprctl
-- reload` resets them the same way, since nothing in this config declares them.
-- Scale 2 there is what silently drove the global XWayland scale (see
-- force_zero_scaling in misc.lua) and 1920x0 strands the cursor off-screen.
-- No headless output present means these hooks simply never fire.
local function pin_headless(mon)
	if not mon or not mon.name or not mon.name:match("^HEADLESS%-") then
		return
	end
	-- Preserve the live mode. sunshine-vdisplay's `attach` resizes this output
	-- to the streaming client's resolution, and clobbering that mid-session
	-- would drop the stream. Only position and scale are invariants.
	local hz = math.floor((mon.refresh_rate or 60) + 0.5)
	hl.monitor({
		output   = mon.name,
		mode     = string.format("%dx%d@%d", mon.width, mon.height, hz),
		position = "9999x0", -- keep in sync with VD_POS   in ~/.local/bin/sunshine-vdisplay
		scale    = 1,        -- keep in sync with VD_SCALE in ~/.local/bin/sunshine-vdisplay
	})
end

hl.on("monitor.added", function(mon)
	pin_headless(mon)
end)

hl.on("config.reloaded", function()
	for _, mon in ipairs(hl.get_monitors() or {}) do
		pin_headless(mon)
	end
end)

-- Resizer listener
hl.on("window.title", function(win)
	local d = {
		hl.dsp.window.float({ action = "on", window = win }),
		hl.dsp.window.center({ window = win }),
	}
	local pip = fn.move_actions(win) or {}

	fn.resizer(win, "Bitwarden", 20, 54, d, true)
	fn.resizer(win, "Picture[- ]in[- ][Pp]icture", 0, 0, pip, false)
end)

hl.on("window.open", function(win)
	local d = {
		hl.dsp.window.float({ action = "on", window = win }),
		hl.dsp.window.center({ window = win }),
	}
	local pip = fn.move_actions(win) or {}

	fn.resizer(win, "Bitwarden", 20, 54, d, true)
	fn.resizer(win, "Picture[- ]in[- ][Pp]icture", 0, 0, pip, false)
end)
