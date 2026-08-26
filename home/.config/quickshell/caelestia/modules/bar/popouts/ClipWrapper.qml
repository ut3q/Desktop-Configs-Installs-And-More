pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import qs.components
import qs.modules.bar.popouts // Need to import this module so the Wrapper type is the same as others

Item {
    id: root

    required property ShellScreen screen
    required property real borderThickness

    readonly property alias content: content
    // `x > 0` was only ever a proxy for "detached", since that is the sole
    // case where x is non-zero. Now that a closed popout parks at a negative x,
    // that proxy would make offsetScale depend on x while x depends on it.
    property real offsetScale: content.isDetached || content.hasCurrent ? 0 : 1

    visible: width > 0 && height > 0
    clip: true

    // Constant width; closing is a pure leftward translation now, not a
    // collapse. The width used to shrink to 0 while the inner Wrapper slid
    // left, which read as the panel deflating from both edges at once.
    implicitWidth: content.implicitWidth
    implicitHeight: content.implicitHeight

    // Closed, the panel parks fully to the left of the screen: its own width,
    // plus the bar's (parent.x IS bar.implicitWidth), plus 5px of slack. The
    // background blob follows for free - ContentWindow measures it off this
    // item - and its right edge lands at -5 for any popout width, because the
    // extraWidth terms cancel: (x + bar - 0.2w) + 1.2w = x + bar + w.
    //
    // The 5 must stay above BlobGroup.smoothing (border.smoothing = 4) or the
    // parked blob necks back into the border frame at the screen edge.
    //
    // nonAnimWidth, not implicitWidth, so the Wrapper's own width animation
    // cannot feed back into x.
    x: content.isDetached ? (parent.width - content.nonAnimWidth) / 2 : content.hasCurrent ? 0 : -(content.nonAnimWidth + parent.x + 5)
    y: {
        if (content.isDetached)
            return (parent.height - content.nonAnimHeight) / 2;

        const off = content.currentCenter - borderThickness - content.nonAnimHeight / 2;
        const diff = parent.height - Math.floor(off + content.nonAnimHeight);
        if (diff < 0)
            return off + diff;
        return Math.max(off, 0);
    }

    Behavior on offsetScale {
        Anim {}
    }

    Behavior on x {
        Anim {
            duration: content.animLength
            easing: content.animCurve
        }
    }

    Behavior on y {
        enabled: root.offsetScale < 1

        Anim {
            duration: content.animLength
            easing: content.animCurve
        }
    }

    Wrapper {
        id: content

        screen: root.screen
        offsetScale: root.offsetScale

        anchors.verticalCenter: parent.verticalCenter
        anchors.left: parent.left
        // The slide lives on the ClipWrapper now; keeping it here too would
        // double the travel.
        anchors.leftMargin: 0
    }
}
