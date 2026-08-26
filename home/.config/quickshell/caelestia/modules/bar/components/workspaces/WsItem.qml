pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import qs.components

// Common base for both workspace delegates.
//
// WsColumn has to ask whichever delegate is current for its height in order to
// size the indicator pill, and it cannot know which of the two kinds it got.
// Giving them a shared type means that lookup is a plain typed property access
// instead of a cast per call site.
ColumnLayout {
    // Flag for finding workspace children
    readonly property bool isWorkspace: true

    // Unanimated target height. `height` animates towards it, but the indicator
    // tracks `size` directly so the pill lands on the new slot instead of
    // trailing the layout there.
    property int size: implicitHeight

    width: ListView.view?.width ?? implicitWidth
    height: size
    spacing: 0

    Behavior on height {
        Anim {}
    }
}
