pragma ComponentBehavior: Bound

import QtQuick
import Caelestia.Config
import qs.components
import qs.components.effects
import qs.services

// The pill that tracks the current workspace.
//
// It clips a recoloured copy of the whole list, so whichever entry it covers
// comes out in the "on" colour without any delegate having to know the pill
// exists. Previously this only understood the main column's Repeater, and the
// picker carried its own near-identical copy inline; it now takes a ListView
// and serves both.
StyledClippingRect {
    id: root

    required property ListView view

    property color colour: Colours.palette.m3primary
    property color onColour: Colours.palette.m3onPrimary

    readonly property real currentY: (view.currentItem as WsItem)?.y ?? 0

    // With activeTrail the two ends chase the target at different speeds, so
    // the pill stretches out of the old slot and settles into the new one.
    property real leading: currentY
    property real trailing: currentY
    property real currentSize: (view.currentItem as WsItem)?.size ?? 0
    property real offset: Math.min(leading, trailing)
    property real size: Math.abs(leading - trailing) + currentSize

    y: offset - view.contentY
    implicitHeight: size
    radius: Tokens.rounding.full
    color: colour

    Colouriser {
        source: root.view
        sourceColor: Colours.palette.m3onSurface
        colorizationColor: root.onColour

        y: -root.y
        implicitWidth: root.view.width
        implicitHeight: root.view.height

        anchors.horizontalCenter: parent.horizontalCenter
    }

    Behavior on leading {
        enabled: root.Config.bar.workspaces.activeTrail

        EAnim {}
    }

    Behavior on trailing {
        enabled: root.Config.bar.workspaces.activeTrail

        EAnim {
            duration: Tokens.anim.durations.normal * 2
        }
    }

    Behavior on currentSize {
        enabled: root.Config.bar.workspaces.activeTrail

        EAnim {}
    }

    Behavior on offset {
        enabled: !root.Config.bar.workspaces.activeTrail

        EAnim {}
    }

    Behavior on size {
        enabled: !root.Config.bar.workspaces.activeTrail

        EAnim {}
    }

    component EAnim: Anim {
        type: Anim.Emphasized
    }
}
