local vars = require("variables")

hl.config({
    general = {
        layout          = "dwindle",

        -- Permission only -- tearing happens solely on windows carrying the
        -- `immediate` rule, which below is scoped to games. The games rule has
        -- always set `immediate`, but with this false it was inert.
        allow_tearing   = true,

        gaps_workspaces = vars.workspaceGaps,
        gaps_in         = vars.windowGapsIn,
        gaps_out        = vars.windowGapsOut,
        border_size     = vars.windowBorderSize,

        col             = {
            active_border   = vars.activeWindowBorderColour,
            inactive_border = vars.inactiveWindowBorderColour,
        },
    },

    dwindle = {
        preserve_split = true,
        smart_split    = false,
        smart_resizing = true,
    },

    scrolling = {
        fullscreen_on_one_column = true,
        focus_fit_method         = 1,
        column_width             = 0.5,
        follow_focus             = true,
        follow_min_visible       = 0.0,
        explicit_column_widths   = "0.35, 0.5, 0.65, 1.0",
    },
})
