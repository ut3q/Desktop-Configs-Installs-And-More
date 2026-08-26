pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Caelestia.Config
import qs.components.containers
import qs.modules.bar as Bar

Scope {
    id: root

    required property ShellScreen screen
    required property Bar.BarWrapper bar

    ExclusionZone {
        anchors.left: true
        exclusiveZone: root.bar.exclusiveZone
    }

    ExclusionZone {
        anchors.top: true
    }

    ExclusionZone {
        anchors.right: true
    }

    ExclusionZone {
        anchors.bottom: true
    }

    component ExclusionZone: StyledWindow {
        screen: root.screen
        name: "border-exclusion"
        exclusiveZone: contentItem.Config.border.thickness

        // A layer surface that reserves nothing still costs a whole
        // QQuickWindow: its own GL context, a QSGRenderThread, and Mesa's
        // per-context worker threads. These four are 1x1 and masked to an
        // empty region -- they exist only to hold an exclusive zone -- so with
        // border.thickness at 0 the top/right/bottom ones were three render
        // threads and three GL contexts drawing one transparent pixel each.
        // Only map the surface once it actually has a zone to reserve; the
        // binding re-evaluates if the thickness is changed at runtime, so the
        // border still appears the moment it is turned on.
        visible: exclusiveZone > 0

        mask: Region {}
        implicitWidth: 1
        implicitHeight: 1
    }
}
