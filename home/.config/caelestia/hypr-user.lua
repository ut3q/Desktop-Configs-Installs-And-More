-- Sunshine game streaming.
--
-- Sunshine captures the physical monitor directly. The virtual-display variant
-- is parked (see ~/Downloads/Moonlight+Apollo/NOTES.md) -- it needs a working
-- moveworkspacetomonitor, which the Lua dispatcher does not provide.
--
-- hl.on returns a subscription, so this handler runs alongside the one in
-- hyprland/execs.lua rather than replacing it.
hl.on("hyprland.start", function()
	local home = os.getenv("HOME")
	hl.exec_cmd("systemctl --user restart sunshine && systemctl --user restart sunshine-scale-watch")
end)

-- Swap Sunshine between mirror mode (this screen, 1920x1080, monitor stays on)
-- and virtual mode (client's native resolution, monitor blanks while streaming).
-- Switching restarts Sunshine, so do it between sessions, not during one.
hl.bind("SUPER + SHIFT + V", hl.dsp.exec_cmd(os.getenv("HOME") .. "/.local/bin/sunshine-mode toggle"))

-- Shell upgrade safety net.
--
-- The shell QML is forked to ~/.config/quickshell/caelestia and pinned to a
-- caelestia-shell version, but the compiled QML plugins it imports live in
-- /usr/lib/qt6/qml and ARE upgraded by pacman. If a future release changes that
-- API, the fork stops loading -- and because it shadows /etc/xdg, the result is
-- no shell at all. This checks a few seconds after login that something came up,
-- and falls back to the packaged build (which always matches the installed
-- plugins) with a loud notification if not.
--
-- Lives here rather than in hyprland/execs.lua on purpose: execs.lua is one of
-- the 25 files `caelestia update` deploys, so edits there can be overwritten.
-- ~/.config/caelestia/hypr-user.lua is not deployed.
-- Shell supervision now lives in the systemd user unit
-- caelestia-shell-supervisor.service (Restart=always), not here. A Hyprland
-- child has nothing watching it, so if the supervisor died the bar quietly
-- stopped being self-healing. The script waits for the compositor itself, so it
-- does not matter whether systemd or Hyprland comes up first.
--   systemctl --user status caelestia-shell-supervisor
--   ~/.config/hypr/scripts/caelestia-shell-supervisor.sh status
