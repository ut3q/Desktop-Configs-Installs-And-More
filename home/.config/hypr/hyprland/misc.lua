local scheme = require("scheme.current")

hl.config({
	misc = {
		vrr = 3,
		animate_manual_resizes = false,
		animate_mouse_windowdragging = false,

		disable_hyprland_logo = true,
		force_default_wallpaper = 0,

		on_focus_under_fullscreen = 2,
		allow_session_lock_restore = true,
		middle_click_paste = false,
		focus_on_activate = true,
		session_lock_xray = true,

		mouse_move_enables_dpms = true,
		key_press_enables_dpms = true,

		background_color = "rgb(" .. scheme.surfaceContainer .. ")",
	},

	render = {
		-- 2 = engage only for fullscreen windows advertising game content type
		-- (render.send_content_type is already true, so the tagging works).
		-- Was 0, which is why `hyprctl monitors` reported DP-3's scanout as
		-- "directScanoutBlockedBy: user settings" -- the compositing path was
		-- never being bypassed for fullscreen games, contrary to the usual
		-- assumption that Hyprland does this automatically.
		direct_scanout = 2,
	},

	xwayland = {
		-- Hyprland gives XWayland ONE global scale: the highest scale of any
		-- monitor. The Sunshine headless output is created at Hyprland defaults
		-- (0x0 physical size -> the auto-DPI heuristic picks scale 2), and
		-- sunshine-remote-scale deliberately scales DP-3 up while streaming.
		-- Either one silently puts every X11 app -- Wine/Roblox Studio via
		-- Vinegar included -- at 2x, to be downscaled back. Pin XWayland to 1:1.
		force_zero_scaling = true,
	},

	debug = {
		error_position = 1,
	},
})
