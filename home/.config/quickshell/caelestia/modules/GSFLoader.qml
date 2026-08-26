import QtQuick
import Quickshell
import Caelestia.Config

// Google Sans Flex is upstream's default UI font, shipped in assets/ because it
// is not packaged. It is also a 3.9MB variable font with six axes, and a bare
// FontLoader made Qt read and register it on every shell start regardless of
// whether anything asked for it. When every family in appearance.font points at
// an installed system font -- as it does here (Rubik / Material Symbols Rounded
// / CaskaydiaCove NF) -- that work was pure startup cost.
//
// Register it only when a configured family actually names it, so switching a
// family back to "Google Sans Flex" still works with no further changes.
Loader {
    id: root

    readonly property var font: GlobalConfig.appearance.font
    readonly property bool wanted: {
        const f = root.font;
        if (!f)
            return false;
        const names = [f.headline?.family, f.title?.family, f.body?.family, f.label?.family, f.mono?.family, f.icon?.family, f.clock, f.workspaces];
        return names.some(n => (n ?? "").toLowerCase().includes("google sans"));
    }

    active: wanted

    sourceComponent: FontLoader {
        source: Quickshell.shellPath("assets/google-sans-flex/GoogleSansFlex-VariableFont_GRAD,ROND,opsz,slnt,wdth,wght.ttf")
    }
}
